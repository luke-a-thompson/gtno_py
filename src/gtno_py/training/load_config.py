import importlib.util
import tomllib
from warnings import warn
from pathlib import Path
from pydantic import BaseModel, model_validator
import torch

from gtno_py.training.config_options import (
    FFNActivation,
    AttentionType,
    MD17MoleculeType,
    MD17Version,
    MD61MoleculeType,
    ModelType,
    NormType,
    OptimizerType,
    RMD17MoleculeType,
    SchedulerType,
    ValueResidualType,
)


class WandbConfig(BaseModel):
    use_wandb: bool


class BenchmarkConfig(BaseModel):
    benchmark_name: str
    model_type: ModelType
    compile: bool
    compile_trace: bool
    runs: int
    log_weights: bool

    @model_validator(mode="before")
    @classmethod
    def validate_benchmark_name(cls, values: dict[str, object]) -> dict[str, object]:
        if values.get("benchmark_name") is None:
            user_input = input("Enter benchmark name (leave blank to use model_type): ")
            if not user_input:
                user_input = str(values.get("model_type"))
            values["benchmark_name"] = user_input
        return values

    @model_validator(mode="after")
    def validate_compile(self) -> "BenchmarkConfig":
        if self.compile and torch.cuda.get_device_capability() < (7, 0):
            raise ValueError("CUDA 7.0 or higher is required to compile the model. We recommend CUDA 11.0 or higher.")
        return self

    @model_validator(mode="after")
    def validate_runs(self) -> "BenchmarkConfig":
        if self.runs < 1:
            raise ValueError("'runs' must be greater than 0.")
        return self

    @model_validator(mode="after")
    def validate_log_weights(self) -> "BenchmarkConfig":
        if self.log_weights:
            if importlib.util.find_spec("matplotlib") is None:
                raise ValueError("If 'log_weights' is True, matplotlib must be installed.")
        return self


class DataloaderConfig(BaseModel):
    multitask: bool
    md17_version: MD17Version
    # Single-task dataloader parameters
    molecule_type: MD17MoleculeType | RMD17MoleculeType | MD61MoleculeType | list[MD17MoleculeType | RMD17MoleculeType | MD61MoleculeType]
    num_timesteps: int

    # Multitask dataloader parameters
    train_molecules: list[MD17MoleculeType | RMD17MoleculeType | MD61MoleculeType] | None = None
    validation_molecules: list[MD17MoleculeType | RMD17MoleculeType | MD61MoleculeType] | None = None
    test_molecules: list[MD17MoleculeType | RMD17MoleculeType | MD61MoleculeType] | None = None

    delta_T: int
    explicit_hydrogen: bool
    explicit_hydrogen_gradients: bool
    radius_graph_threshold: float
    rrwp_length: int
    normalize_z: bool
    persistent_workers: bool
    num_workers: int
    pin_memory: bool
    prefetch_factor: int
    force_regenerate: bool

    @model_validator(mode="after")
    def validate_singletask(self) -> "DataloaderConfig":
        if not self.multitask and self.molecule_type is None:
            raise ValueError("If 'multitask' is False, 'molecule_type' must be specified.")

        if self.multitask is True and not (self.train_molecules or self.validation_molecules or self.test_molecules):
            raise ValueError(
                "If 'multitask' is True, 'train_molecules', 'validation_molecules', and 'test_molecules' must be specified. They are only used for multitask dataloaders."
            )
        return self

    @model_validator(mode="after")
    def validate_multitask(self) -> "DataloaderConfig":
        if self.multitask and (not self.train_molecules or not self.validation_molecules or not self.test_molecules):
            raise ValueError("If 'multitask' is True, 'train_molecules', 'validation_molecules', and 'test_molecules' must be specified.")

        return self

    @model_validator(mode="after")
    def validate_train_molecules(self) -> "DataloaderConfig":
        if self.multitask and self.train_molecules and self.validation_molecules and self.test_molecules:
            # Check for shared molecules between train, validation, and test sets
            train_set = set(self.train_molecules)
            val_set = set(self.validation_molecules)
            test_set = set(self.test_molecules)
            if train_set.intersection(val_set):
                warn(f"Train and validation molecule sets overlap: {', '.join(str(mol) for mol in train_set.intersection(val_set))}")

            if train_set.intersection(test_set):
                warn(f"Train and test molecule sets overlap: {', '.join(str(mol) for mol in train_set.intersection(test_set))}.")

            if val_set.intersection(test_set):
                warn(f"Validation and test molecule sets overlap: {', '.join(str(mol) for mol in val_set.intersection(test_set))}")

        return self

    @model_validator(mode="after")
    def validate_md17_version(self) -> "DataloaderConfig":
        """Validate that the molecule types match the MD17 version."""

        # Convert string molecules to appropriate enum type based on version
        match self.md17_version:
            case MD17Version.md17:
                enum_type = MD17MoleculeType
            case MD17Version.rmd17:
                enum_type = RMD17MoleculeType
            case MD17Version.md61:
                enum_type = MD61MoleculeType
            case _:
                raise ValueError(f"Invalid MD17 version: {self.md17_version}")

        # Convert single-task molecule types
        if isinstance(self.molecule_type, list):
            self.molecule_type = [enum_type(m) for m in self.molecule_type]
        else:
            self.molecule_type = enum_type(self.molecule_type)

        # Convert multitask molecule lists
        if self.train_molecules:
            self.train_molecules = [enum_type(m) for m in self.train_molecules]
        if self.validation_molecules:
            self.validation_molecules = [enum_type(m) for m in self.validation_molecules]
        if self.test_molecules:
            self.test_molecules = [enum_type(m) for m in self.test_molecules]

        return self

    @model_validator(mode="after")
    def validate_explicit_hydrogen_gradients(self) -> "DataloaderConfig":
        if self.explicit_hydrogen_gradients and not self.explicit_hydrogen:
            raise ValueError(
                "If 'explicit_hydrogen_gradients' is True, 'explicit_hydrogen' must also be True. You cannot calculate the gradients for hydrogen atoms without them being present in the graph."
            )
        return self


class TrainingConfig(BaseModel):
    device: torch.device
    seed: int
    batch_size: int
    epochs: int
    max_grad_norm: float
    learned_label_noise: bool
    label_noise_std: float

    class Config:
        arbitrary_types_allowed = True

    @model_validator(mode="after")
    def validate_brownian_noise_std(self) -> "TrainingConfig":
        if self.label_noise_std < 0.0:
            raise ValueError("'brownian_noise_std' must be 0.0 or greater.")
        return self

    @model_validator(mode="before")
    @classmethod
    def convert_device_to_torch_device(cls, values: dict[str, object]) -> dict[str, object]:
        device_value = values.get("device")
        if device_value is not None:
            if not isinstance(device_value, (str, int, torch.device)):
                raise ValueError(f"Invalid type for device: {device_value}")

            try:
                values["device"] = torch.device(device_value)
            except (TypeError, ValueError) as e:
                raise ValueError(f"Could not convert {device_value} to torch.device: {e}")
        return values


class OptimizerConfig(BaseModel):
    type: OptimizerType
    learning_rate: float
    weight_decay: float
    adam_betas: tuple[float, float]
    adam_eps: float


class SchedulerConfig(BaseModel):
    type: SchedulerType


class GTNOConfig(BaseModel):
    # Architecture parameters
    num_layers: int
    num_heads: int
    lifting_dim: int
    # Output parameters
    output_heads: int
    delta_update: bool
    # Attention parameters
    heterogenous_attention_type: AttentionType
    use_rope: bool
    learnable_attention_denom: bool
    # Feature parameters
    use_spherical_harmonics: bool
    use_equivariant_lifting: bool
    # Layer parameters
    norm: NormType
    activation: FFNActivation
    value_residual_type: ValueResidualType

    @model_validator(mode="after")
    def validate_output_heads(self) -> "GTNOConfig":
        if self.output_heads < 1:
            raise ValueError("'output_heads' must be greater than 0.")
        return self

    @model_validator(mode="after")
    def validate_lifting_dim_and_num_heads(self) -> "GTNOConfig":
        if self.lifting_dim % self.num_heads != 0:
            raise ValueError("'lifting_dim' must be divisible by 'num_heads'.")
        return self


class EGNOConfig(BaseModel):
    num_layers: int
    lifting_dim: int
    activation: FFNActivation
    normalise_scalars: bool
    use_time_conv: bool
    num_fourier_modes: int
    time_embed_dim: int

    @model_validator(mode="after")
    def validate_lifting_dim(self) -> "EGNOConfig":
        if self.lifting_dim % 2 != 0:
            raise ValueError("'lifting_dim' must be even.")
        return self

    @model_validator(mode="after")
    def validate_num_fourier_modes(self) -> "EGNOConfig":
        if self.num_fourier_modes < 1:
            raise ValueError("'num_fourier_modes' must be greater than 0.")
        return self


class Config(BaseModel):
    wandb: WandbConfig
    benchmark: BenchmarkConfig
    dataloader: DataloaderConfig
    training: TrainingConfig
    optimizer: OptimizerConfig
    scheduler: SchedulerConfig
    gtno_config: GTNOConfig
    egno_config: EGNOConfig

    @model_validator(mode="after")
    def validate_output_heads(self) -> "Config":
        if self.gtno_config.output_heads > 1 and self.dataloader.multitask is False:
            warn("Are you sure you want to use multiple output heads for a single-task model? This is unusual, but maybe you're onto something.")
        return self

    @classmethod
    def from_toml(cls, path: Path, skip_model_naming: bool = False) -> "Config":
        """
        Load configuration from a TOML file.

        Args:
            path: Path to the TOML file

        Returns:
            Config: Validated configuration object
        """
        with open(path, "rb") as f:
            config_dict = tomllib.load(f)

        return cls(**config_dict)
