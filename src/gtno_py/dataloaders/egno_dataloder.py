import numpy as np
import numpy.typing as npt
import torch
import pickle as pkl
from torch.utils.data import Dataset, DataLoader
import os
from enum import Enum
from typing import final, override
from tensordict import TensorDict
from pathlib import Path


@final
class DataPartition(str, Enum):
    train = "train"
    val = "val"
    test = "test"


@final
class MD17Version(str, Enum):
    md17 = "md17"
    rmd17 = "rmd17"


@final
class MD17MoleculeType(str, Enum):
    aspirin = "aspirin"
    benzene = "benzene"
    ethanol = "ethanol"
    malonaldehyde = "malonaldehyde"
    naphthalene = "naphthalene"
    salicylic = "salicylic"
    toluene = "toluene"
    uracil = "uracil"


@final
class RMD17MoleculeType(str, Enum):
    azobenzene = "azobenzene"
    benzene = "benzene"
    ethanol = "ethanol"
    malonaldehyde = "malonaldehyde"
    naphthalene = "naphthalene"
    paracetamol = "paracetamol"
    salicylic = "salicylic"
    toluene = "toluene"
    uracil = "uracil"


class MD17Dataset(Dataset[dict[str, torch.Tensor]]):
    """
    MD17 Dataset
    """

    def __init__(
        self,
        partition: DataPartition,
        max_samples: int,
        delta_frame: int,
        data_dir: str,
        split_dir: str,
        md17_version: MD17Version,
        molecule_type: MD17MoleculeType,
        train_par: float = 0.1,
        val_par: float = 0.05,
        test_par: float = 0.05,
        seed: int = 100,
        force_regenerate: bool = False,
        num_timesteps: int = 1,  # Number of timesteps to replicate
    ):
        """
        Args:
            partition (str): The partition to load ('train', 'val', 'test').
            max_samples (int): The maximum number of samples to load into the initial frame.
            delta_frame (int): The number of frames to skip between the initial and target frames.
            data_dir (str): The directory which stores the MD17 and RMD17 data.
            split_dir (str): The directory to load or store splits.
            molecule_type (str): The type of molecule to load ('aspirin', 'benzene_old', 'ethanol', 'malonaldehyde', 'naphthalene', 'salicylic', 'toluene', 'uracil').
            train_par (float): The percentage of the data to use for training.
            val_par (float): The percentage of the data to use for validation.
            test_par (float): The percentage of the data to use for testing.
            num_timesteps (int): Number of timesteps for replication.
        """
        self.partition: DataPartition = partition
        self.md17_version: MD17Version = md17_version
        self.molecule_type: MD17MoleculeType = molecule_type
        self.delta_frame: int = delta_frame
        self.num_timesteps: int = num_timesteps
        self.max_samples: int = max_samples

        self.dft_imprecision_margin: int = 10_000

        match md17_version:
            case MD17Version.md17:
                full_dir = os.path.join(data_dir + "md17_npz/" + "md17_" + molecule_type + ".npz")
                split_dir = os.path.join(split_dir + "md17_splits/" + "md17_" + molecule_type + "_split.pkl")
                positions_col = "R"
                charges_col = "z"
            case MD17Version.rmd17:
                full_dir = os.path.join(data_dir + "rmd17_npz/" + "rmd17_" + molecule_type + ".npz")
                split_dir = os.path.join(split_dir + "rmd17_splits/" + "rmd17_" + molecule_type + "_split.pkl")
                positions_col = "coords"
                charges_col = "nuclear_charges"
            case _:
                raise ValueError(f"Invalid MD17 version: {md17_version}")

        data_file: np.lib.npyio.NpzFile = np.load(full_dir)
        self.x: npt.NDArray[np.float64] = data_file[positions_col]
        self.z: npt.NDArray[np.uint8] = data_file[charges_col]
        self.v: npt.NDArray[np.float64] = self.x[1:] - self.x[:-1]  # Construct velocities from successive coords
        self.x = self.x[:-1]  # Remove last coord to ensure len(x) == len(v)
        assert self.x.shape == self.v.shape

        split = self._get_or_generate_split(
            split_dir=split_dir,
            x=self.x,
            train_par=train_par,
            val_par=val_par,
            test_par=test_par,
            force_regenerate=force_regenerate,
            seed=seed,
        )

        match partition:
            case DataPartition.train:
                split_times = split[0]
            case DataPartition.val:
                split_times = split[1]
            case DataPartition.test:
                split_times = split[2]
            case _:
                raise ValueError(f"Invalid partition: {partition}")

        split_times = split_times[:max_samples]
        self.split_times = split_times

        heavy_atom_mask = self.z > 1
        self.x = self.x[:, heavy_atom_mask, ...]
        self.v = self.v[:, heavy_atom_mask, ...]
        self.z = self.z[heavy_atom_mask]

        self.process_data(split_times, self.x, self.v, self.z)

        # --- Precompute Replication ---
        self.replicated_x_0: torch.Tensor = self._replicate_tensor(self.x_0)
        self.replicated_v_0: torch.Tensor = self._replicate_tensor(self.v_0)
        self.replicated_concatenated_features: torch.Tensor = self._replicate_tensor(self.concatenated_features)

    def process_data(self, split_times: npt.NDArray[np.int_], x: npt.NDArray[np.float64], v: npt.NDArray[np.float64], z: npt.NDArray[np.uint8]):
        """Processes loaded data, common to both MD17Dataset and MD17DynamicsDataset"""
        x_0, v_0 = self.get_initial_frames(split_times, x, v)
        x_t, v_t = self.get_target_frames(split_times, x, v)

        mole_idx = z
        n_node = mole_idx.shape[0]
        self.n_node: int = n_node

        one_hop_adjacency, two_hop_adjacency = self._compute_adjacency_matrix(x, n_node)
        edge_attr, edges = self._build_edge_attributes(one_hop_adjacency, two_hop_adjacency, mole_idx, x_0, v_0)
        self.edge_attr: torch.Tensor = edge_attr
        self.edges = edges

        all_edges = self._compute_all_edges(x=x, z=z)
        conf_edges = self._compute_conf_edges(all_edges=all_edges)
        self.conf_edges = conf_edges

        self.x_0: torch.Tensor = torch.cat([torch.Tensor(x_0), torch.norm(torch.Tensor(x_0), dim=-1, keepdim=True)], dim=-1)
        self.v_0: torch.Tensor = torch.cat([torch.Tensor(v_0), torch.norm(torch.Tensor(v_0), dim=-1, keepdim=True)], dim=-1)
        self.mole_idx: torch.Tensor = torch.Tensor(mole_idx)
        self.Z: torch.Tensor = torch.Tensor(z)
        if self.md17_version == MD17Version.md17:
            self.cfg = self.sample_cfg()

        if x_t is not None:
            self.x_t: torch.Tensor = torch.Tensor(x_t)
        if v_t is not None:
            self.v_t: torch.Tensor = torch.Tensor(v_t)

        self.concatenated_features: torch.Tensor = self._compute_concatenated_features()

    def _compute_concatenated_features(self) -> torch.Tensor:
        """Pre-compute concatenated features for all samples."""
        x_0_xyz = self.x_0[..., :3]
        v_0_xyz = self.v_0[..., :3]
        x_0_norm = self.x_0[..., 3:]
        v_0_norm = self.v_0[..., 3:]
        Z_unsqueeze = self.Z.unsqueeze(-1)

        concatenated_features = torch.cat(
            [
                x_0_xyz,
                x_0_norm,
                v_0_xyz,
                v_0_norm,
                Z_unsqueeze.expand(self.x_0.shape[0], -1, -1),
            ],
            dim=-1,
        )
        return concatenated_features

    def _replicate_tensor(self, tensor: torch.Tensor) -> torch.Tensor:
        """
        Replicates a single tensor along the batch dimension.

        Input tensor shape: [max_samples, d]
        Output tensor shape: [max_samples * num_timesteps, d]

        Returns:
            torch.Tensor: The replicated tensor.
        """
        # Add new time dimension
        assert tensor.shape[0] == self.max_samples, f"Tensor shape: {tensor.shape}, max_samples: {self.max_samples}"
        tensor_with_time = tensor.unsqueeze(1)

        # Expand along time dimension to num_timesteps
        tensor_expanded = tensor_with_time.expand(-1, self.num_timesteps, *tensor.shape[1:])

        # Reshape to flatten batch and time dimensions
        tensor_reshaped = tensor_expanded.reshape(-1, *tensor.shape[1:])
        assert tensor_reshaped.shape == (self.max_samples * self.num_timesteps, *tensor.shape[1:])

        return tensor_reshaped

    def get_initial_frames(
        self, split_times: npt.NDArray[np.int_], x: npt.NDArray[np.float64], v: npt.NDArray[np.float64]
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        x_0 = x[split_times]
        v_0 = v[split_times]
        return x_0, v_0

    def get_target_frames(
        self, split_times: npt.NDArray[np.int_], x: npt.NDArray[np.float64], v: npt.NDArray[np.float64]
    ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.float64]]:
        x_t = x[split_times + self.delta_frame]
        v_t = v[split_times + self.delta_frame]
        return x_t, v_t

    def _get_or_generate_split(
        self,
        split_dir: Path,
        x: npt.NDArray[np.float64],
        train_par: float,
        val_par: float,
        test_par: float,
        seed: int,
        force_regenerate: bool = False,
    ) -> tuple[npt.NDArray[np.int_], npt.NDArray[np.int_], npt.NDArray[np.int_]]:
        start = self.dft_imprecision_margin
        end = x.shape[0] - self.dft_imprecision_margin - self.delta_frame + 1

        try:
            if force_regenerate:
                raise FileNotFoundError("Force regeneration of dataset")
            with open(split_dir, "rb") as f:
                split = pkl.load(f)
        except FileNotFoundError:
            print("Error loading split file, regenerating split")
            np.random.seed(seed)
            x_middle = x[start:end]
            num_timesteps = x_middle.shape[0]
            train_idx = np.random.choice(np.arange(num_timesteps), size=int(train_par * num_timesteps), replace=False)
            flag = np.zeros(num_timesteps)
            for _ in train_idx:
                flag[_] = 1
            rest = [_ for _ in range(num_timesteps) if not flag[_]]
            val_idx = np.random.choice(rest, size=int(val_par * num_timesteps), replace=False)
            for _ in val_idx:
                flag[_] = 1
            rest = [_ for _ in range(num_timesteps) if not flag[_]]
            test_idx = np.random.choice(rest, size=int(test_par * num_timesteps), replace=False)

            # Add back first frames to keep within original frame range
            train_idx += start
            val_idx += start
            test_idx += start
            split = (train_idx, val_idx, test_idx)
            with open(split_dir, "wb") as f:
                pkl.dump(split, f)
            print("Generate and save split!")
        return split

    def _compute_adjacency_matrix(self, x: npt.NDArray[np.float64], num_atoms: int, threshold: float = 1.6) -> tuple[npt.NDArray[np.int_], npt.NDArray[np.int_]]:
        def l2_dist(_i: int, _j: int, _t: int) -> float:
            """Compute the distance between two atoms at a given time step."""
            return np.sqrt(np.sum((x[_t][_i] - x[_t][_j]) ** 2))

        one_hop_edges = torch.zeros(num_atoms, num_atoms).int()
        for i in range(num_atoms):
            for j in range(num_atoms):
                if i != j:
                    distance = l2_dist(i, j, 0)
                    if distance < threshold:
                        one_hop_edges[i][j] = 1

        two_hop_edges = (one_hop_edges @ one_hop_edges).clamp(max=1)
        assert one_hop_edges.shape == two_hop_edges.shape == (num_atoms, num_atoms)
        return one_hop_edges, two_hop_edges

    def _build_edge_attributes(self, one_hop_adjacency, two_hop_adjacency, mole_idx, x_0, v_0):
        n_node = mole_idx.shape[0]
        edge_attr = []
        rows = []
        cols = []

        x_0_frame_one = x_0[0]  # use frame 0 to compute edges

        for i in range(n_node):
            for j in range(n_node):
                if i == j:
                    continue
                else:
                    first_frame_distance = np.linalg.norm(x_0_frame_one[i] - x_0_frame_one[j])
                    if one_hop_adjacency[i][j]:
                        rows.append(i)
                        cols.append(j)
                        edge_attr.append([mole_idx[i], mole_idx[j], 1, first_frame_distance])
                        assert not two_hop_adjacency[i][j]
                    if two_hop_adjacency[i][j]:
                        rows.append(i)
                        cols.append(j)
                        edge_attr.append([mole_idx[i], mole_idx[j], 2, first_frame_distance])
                        assert not one_hop_adjacency[i][j]

        edges = [rows, cols]
        edge_attr_tensor = torch.Tensor(np.array(edge_attr))
        return edge_attr_tensor, edges

    def _compute_all_edges(self, x, z, threshold=1.6):
        from collections import defaultdict

        n = z.shape[0]
        positions = x[0]
        i_indices, j_indices = np.triu_indices(n, k=1)
        deltas = positions[i_indices] - positions[j_indices]
        distances = np.sqrt(np.einsum("ij,ij->i", deltas, deltas))
        mask = distances < threshold
        valid_i = i_indices[mask]
        valid_j = j_indices[mask]
        type_pairs = np.sort(np.column_stack([z[valid_i], z[valid_j]]), axis=1)
        all_edges = defaultdict(list)
        for (a, b), i, j in zip(type_pairs, valid_i, valid_j):
            all_edges[(a, b)].append([int(i), int(j)])
        return dict(all_edges)

    def _compute_conf_edges(self, all_edges):
        conf_edges = []
        for key in all_edges:
            conf_edges.extend(all_edges[key])
        return conf_edges

    def sample_cfg(self):
        cfg = {}
        if self.molecule_type == MD17MoleculeType.benzene:
            cfg["Stick"] = [(0, 1), (2, 3), (4, 5)]
        elif self.molecule_type == MD17MoleculeType.aspirin:
            cfg["Stick"] = [(0, 2), (1, 3), (5, 6), (7, 10), (11, 12)]
        elif self.molecule_type == MD17MoleculeType.ethanol:
            cfg["Stick"] = [(0, 1)]
        elif self.molecule_type == MD17MoleculeType.malonaldehyde:
            cfg["Stick"] = [(1, 2)]
        elif self.molecule_type == MD17MoleculeType.naphthalene:
            cfg["Stick"] = [(0, 1), (2, 3), (4, 9), (5, 6), (7, 8)]
        elif self.molecule_type == MD17MoleculeType.salicylic:
            cfg["Stick"] = [(0, 9), (1, 2), (4, 5), (6, 7)]
        elif self.molecule_type == MD17MoleculeType.toluene:
            cfg["Stick"] = [(2, 3), (5, 6), (0, 1)]
        elif self.molecule_type == MD17MoleculeType.uracil:
            cfg["Stick"] = [(0, 1), (3, 4)]
        else:
            raise NotImplementedError()
        cur_selected = []
        for _ in cfg["Stick"]:
            cur_selected.append(_[0])
            cur_selected.append(_[1])
        cfg["Isolated"] = [[_] for _ in range(self.n_node) if _ not in cur_selected]
        if len(cfg["Isolated"]) == 0:
            cfg.pop("Isolated")
        return cfg

    @override
    def __getitem__(self, i: int) -> dict[str, torch.Tensor]:
        """
        Retrieve the i-th sample, including its replicated time steps, from the dataset.

        The base tensors (e.g. x_0, v_0, concatenated_features) are precomputed and stored in a
        flattened form with shape (max_samples * num_timesteps, N, d). This flattening is performed
        by first expanding each tensor to have an explicit time dimension (resulting in shape
        (max_samples, num_timesteps, N, d)) and then reshaping it to merge the sample and time dimensions.

        For a given sample index i, slicing from index (i * num_timesteps) to ((i + 1) * num_timesteps)
        retrieves the contiguous block corresponding to that sample’s time steps. This operation recovers
        the time dimension, yielding a tensor of shape (num_timesteps, N, d).

        In addition, target tensors (x_t and v_t), which are originally of shape
        (max_samples, N, num_timesteps, d), are transposed so that the time dimension is first,
        resulting in shape (num_timesteps, N, d).

        Returns:
            dict[str, torch.Tensor]: A dictionary containing:
                - "x_0": Tensor of initial positions with shape (num_timesteps, N, d)
                - "v_0": Tensor of initial velocities with shape (num_timesteps, N, d)
                - "x_t": Tensor of target positions with shape (num_timesteps, N, d)
                - "v_t": Tensor of target velocities with shape (num_timesteps, N, d)
                - "concatenated_features": Tensor of concatenated features with shape (num_timesteps, N, d)
        """
        assert len(self.replicated_x_0) == self.max_samples * self.num_timesteps
        # For sample index i, slice out the contiguous block of timesteps (of size num_timesteps)
        # from the pre-replicated tensors. This recovers the T timesteps associated with the i-th sample.
        # i * self.num_timesteps : (i + 1) * self.num_timesteps - We want to be this many i * timesteps *frames* from the start, and capture the whole frame
        return {
            "x_0": self.replicated_x_0[i * self.num_timesteps : (i + 1) * self.num_timesteps],
            "v_0": self.replicated_v_0[i * self.num_timesteps : (i + 1) * self.num_timesteps],
            "x_t": self.x_t[i].transpose(0, 1),
            "v_t": self.v_t[i].transpose(0, 1),
            "concatenated_features": self.replicated_concatenated_features[i * self.num_timesteps : (i + 1) * self.num_timesteps],
        }

    def __len__(self):
        return len(self.split_times)

    def get_edges(self, batch_size: int, n_nodes: int) -> tuple[torch.LongTensor, torch.LongTensor]:
        edges = (torch.LongTensor(self.edges[0]), torch.LongTensor(self.edges[1]))
        if batch_size == 1:
            return edges

        rows, cols = [], []
        for i in range(batch_size):
            rows.append(edges[0] + n_nodes * i)
            cols.append(edges[1] + n_nodes * i)
        edges = (torch.cat(rows).long(), torch.cat(cols).long())
        return edges

    @staticmethod
    def get_cfg(batch_size: int, n_nodes: int, cfg: TensorDict) -> TensorDict:
        offset = torch.arange(batch_size, device=cfg.device) * n_nodes
        for bond_type in cfg.keys():
            index = cfg.get(bond_type)
            index = index + offset.unsqueeze(-1).unsqueeze(-1).expand_as(index)
            if bond_type == "Isolated":
                index = index.squeeze(-1)
            cfg.set(bond_type, index)
        return cfg


@final
class MD17DynamicsDataset(MD17Dataset):
    """
    MD17 Dynamics Dataset
    """

    def __init__(
        self,
        partition: DataPartition,
        max_samples: int,
        delta_frame: int,
        data_dir: str,
        split_dir: str,
        md17_version: MD17Version,
        molecule_type: MD17MoleculeType,
        train_par: float = 0.1,
        val_par: float = 0.05,
        test_par: float = 0.05,
        num_timesteps: int = 8,  # Number of timesteps for dynamics
        seed: int = 100,
        force_regenerate: bool = False,
    ):
        super().__init__(
            partition=partition,
            max_samples=max_samples,
            delta_frame=delta_frame,
            data_dir=data_dir,
            split_dir=split_dir,
            md17_version=md17_version,
            molecule_type=molecule_type,
            train_par=train_par,
            val_par=val_par,
            test_par=test_par,
            seed=seed,
            force_regenerate=force_regenerate,
            num_timesteps=num_timesteps,  # Pass num_timesteps to base class for replication
        )
        self.x_t, self.v_t = self.get_dynamic_target_frames()

        self.x_t = torch.Tensor(self.x_t)
        self.v_t = torch.Tensor(self.v_t)

        # Re-replicate dataset after defining x_t, v_t for dynamics dataset
        self.replicated_x_0: torch.Tensor = self._replicate_tensor(self.x_0)
        self.replicated_v_0: torch.Tensor = self._replicate_tensor(self.v_0)
        self.replicated_concatenated_features: torch.Tensor = self._replicate_tensor(self.concatenated_features)

    def get_dynamic_target_frames(self):
        split_times = self.split_times
        delta_frame = self.delta_frame
        num_timesteps = self.num_timesteps

        x_t_list = [self.x[split_times + delta_frame * i // num_timesteps] for i in range(1, num_timesteps + 1)]
        x_t = np.stack(x_t_list, axis=2)
        v_t_list = [self.v[split_times + delta_frame * i // num_timesteps] for i in range(1, num_timesteps + 1)]
        v_t = np.stack(v_t_list, axis=2)
        return x_t, v_t

    @override
    def get_target_frames(self, split_times, x, v):
        return None, None  # No single frame target for dynamics, handled by get_dynamic_target_frames


if __name__ == "__main__":
    import time
    from tqdm import tqdm

    # Test MD17Dataset
    dataset_static = MD17Dataset(
        partition=DataPartition.train,
        max_samples=5000,
        delta_frame=30000,
        data_dir="data/",
        split_dir="data/",
        md17_version=MD17Version.rmd17,
        molecule_type=MD17MoleculeType.benzene,
        force_regenerate=False,
        num_timesteps=1,  # Set num_timesteps for replication
    )
    dataloader_static = DataLoader(dataset_static, batch_size=100, shuffle=True)
    # print("MD17Dataset Output Shapes:")
    # for data in dataloader_static:
    #     for key in data:
    #         if key not in ["cfg", "edge_attr"]:
    #             print(f"  {key}:", data[key].shape)
    #     if "cfg" in data:
    #         print("  cfg shapes:")
    #         for key in data["cfg"]:
    #             print(f"    {key}:", data["cfg"][key].shape)
    #     break

    # Test MD17DynamicsDataset
    dataset_dynamic = MD17DynamicsDataset(
        partition=DataPartition.train,
        max_samples=500,
        delta_frame=3000,
        data_dir="data/",
        split_dir="data/",
        md17_version=MD17Version.md17,
        molecule_type=MD17MoleculeType.toluene,
        force_regenerate=False,
        num_timesteps=8,  # Set num_timesteps for replication
    )

    dataloader_dynamic = DataLoader(dataset_dynamic, batch_size=100, shuffle=True)

    # Warm-up iterations
    for _ in range(500):
        next(iter(dataloader_dynamic))

    # Benchmarking with statistics
    times: list[float] = []
    num_batches: int = 10_000

    for rep in tqdm(range(100)):
        start_time = time.time()
        for i, batch in enumerate(dataloader_dynamic):
            if i == num_batches:
                break
        elapsed = time.time() - start_time
        times.append(elapsed)

    mean_time = np.mean(times)
    std_time = np.std(times)
    print(f"Batch overhead - Mean: {mean_time:.4f} s, Std: {std_time:.4f} s")
    print(f"Latex: \\({mean_time:.3f}{{\\scriptstyle \\pm{std_time:.3f}}}\\)")

    # print("\nMD17DynamicsDataset Output Shapes:")
    # for data in dataloader_dynamic:
    #     for key in data:
    #         if key not in ["cfg", "edge_attr"]:
    #             print(f"  {key}:", data[key].shape)
    #     if "cfg" in data:
    #         print("  cfg shapes:")
    #         for key in data["cfg"]:
    #             print(f"    {key}:", data["cfg"][key].shape)
    #     break
