import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
import torch
import torch.nn.functional as F
import tensordict
from gtno_py.utils import add_brownian_noise
from gtno_py.training.load_config import Config


def train_epoch(
    config: Config,
    model: nn.Module,
    optimizer: optim.Optimizer,
    dataloader: DataLoader[dict[str, torch.Tensor]],
    scheduler: optim.lr_scheduler._LRScheduler | None,
) -> float:
    """Single training epoch.

    Args:
        config (Config): The configuration file.
        model (nn.Module): The model to train.
        optimizer (optim.Optimizer): The optimizer to use.
        dataloader (DataLoader[dict[str, torch.Tensor]]): The dataloader to use.
        scheduler (optim.lr_scheduler._LRScheduler | None): The scheduler to use.

    Returns:
        float: The loss of the epoch.
    """
    _ = model.train()
    total_loss = 0.0

    for batch in dataloader:
        batch = tensordict.from_dict(batch).to(config.training.device)
        assert batch["x_0"].shape[1] == (config.model.num_timesteps), batch["x_0"].shape
        target_coords: torch.Tensor = batch.pop("x_t")
        _ = batch.pop("v_t")

        if config.training.learnable_noise_std:
            batch["x_0"], batch["v_0"], batch["concatenated_features"] = add_brownian_noise(
                batch["x_0"],
                batch["v_0"],
                batch["concatenated_features"],
                config.training.brownian_noise_std,
            )

        optimizer.zero_grad()
        pred_coords: torch.Tensor = model(batch)

        # Calculate MSE loss
        assert pred_coords.shape == target_coords.shape, f"{pred_coords.shape} != {target_coords.shape}"

        # Do not compute gradients for heavy atoms if explicit_hydrogen is True and explicit_hydrogen_gradients is False
        if config.dataloader.explicit_hydrogen and config.dataloader.explicit_hydrogen_gradients is False:
            heavy_atom_mask: torch.Tensor = batch["Z"][..., 0] > 1  # shape: [Batch, Time, Nodes]

            # Apply mask along the nodes dimension
            pred_heavy: torch.Tensor = pred_coords[heavy_atom_mask]  # shape: [Total_selected_nodes, 3]
            target_heavy: torch.Tensor = target_coords[heavy_atom_mask]  # shape: [Total_selected_nodes, 3]

            loss = F.mse_loss(pred_heavy, target_heavy)
        # Compute gradients for all atoms (heavy and hydrogen)
        else:
            # Compute element‐wise MSE loss without reduction.
            loss_raw = F.mse_loss(pred_coords, target_coords, reduction="none")
            # Mask is [B,T,N,H]
            # Apply mask and compute average loss over valid nodes.
            loss = (loss_raw * batch["padded_nodes_mask"]).sum() / batch["padded_nodes_mask"].sum()

        total_loss += loss.item() * batch.batch_size[0]

        _ = loss.backward()
        _ = torch.nn.utils.clip_grad_norm_(model.parameters(), config.training.max_grad_norm)
        optimizer.step()

        if scheduler and not isinstance(scheduler, optim.lr_scheduler.ReduceLROnPlateau):
            scheduler.step()

    return total_loss / float(len(dataloader.dataset))


def eval_epoch(
    config: Config,
    model: nn.Module,
    loader: DataLoader[dict[str, torch.Tensor]],
) -> tuple[float, float]:
    """Evaluation loop.

    Args:
        config (Config): The configuration file.
        model (nn.Module): The model to evaluate.
        loader (DataLoader[dict[str, torch.Tensor]]): The dataloader to use.

    Returns:
        tuple[float, float]: The S2T and S2S loss of the epoch.
    """
    model.eval()
    total_s2t_loss = 0.0
    total_s2s_loss = 0.0

    with torch.no_grad():
        for batch in loader:
            batch = tensordict.from_dict(batch).to(config.training.device)
            target_coords = batch.pop("x_t")
            _ = batch.pop("v_t")

            pred_coords: torch.Tensor = model(batch)

            if config.dataloader.explicit_hydrogen and config.dataloader.explicit_hydrogen_gradients is False:
                # Get atomic numbers Z from batch and create mask for heavy atoms (Z > 1)
                heavy_atom_mask: torch.Tensor = batch["Z"][..., 0] > 1  # shape: [Batch, Time, Nodes]

                # Apply mask along the nodes dimension
                pred_heavy: torch.Tensor = pred_coords[heavy_atom_mask]  # shape: [Total_selected_nodes, 3]
                target_heavy: torch.Tensor = target_coords[heavy_atom_mask]  # shape: [Total_selected_nodes, 3]

                s2t_loss = F.mse_loss(pred_heavy, target_heavy)
                s2s_loss = F.mse_loss(pred_heavy[:, -1, :, :], target_heavy[:, -1, :, :])
                total_s2t_loss += s2t_loss.item() * batch.batch_size[0]
                total_s2s_loss += s2s_loss.item() * batch.batch_size[0]
            else:
                # For the full coordinates loss (shape: [batch, 8, 20, 4])
                loss_raw_s2t = F.mse_loss(pred_coords, target_coords, reduction="none")
                masked_s2t_loss = (loss_raw_s2t * batch["padded_nodes_mask"]).sum() / batch["padded_nodes_mask"].sum()

                # For the last slice loss (shape: [batch, 20, 4])
                loss_raw_s2s = F.mse_loss(pred_coords[:, -1, :, :], target_coords[:, -1, :, :], reduction="none")
                masked_s2s_loss = (loss_raw_s2s * batch["padded_nodes_mask"]).sum() / batch["padded_nodes_mask"].sum()

                total_s2t_loss += masked_s2t_loss.item() * batch.batch_size[0]
                total_s2s_loss += masked_s2s_loss.item() * batch.batch_size[0]

    return total_s2t_loss / len(loader.dataset), total_s2s_loss / len(loader.dataset)
