from enum import StrEnum
from typing import final


@final
class DataPartition(StrEnum):
    train = "train"
    val = "val"
    test = "test"


@final
class MD17Version(StrEnum):
    md17 = "md17"
    rmd17 = "rmd17"


@final
class MD17MoleculeType(StrEnum):
    aspirin = "aspirin"
    benzene = "benzene"
    ethanol = "ethanol"
    malonaldehyde = "malonaldehyde"
    naphthalene = "naphthalene"
    salicylic = "salicylic"
    toluene = "toluene"
    uracil = "uracil"


@final
class RMD17MoleculeType(StrEnum):
    azobenzene = "azobenzene"
    benzene = "benzene"
    ethanol = "ethanol"
    malonaldehyde = "malonaldehyde"
    naphthalene = "naphthalene"
    paracetamol = "paracetamol"
    salicylic = "salicylic"
    toluene = "toluene"
    uracil = "uracil"


@final
class DeviceType(StrEnum):
    CPU = "cpu"
    CUDA = "cuda"


@final
class OptimizerType(StrEnum):
    SGD = "sgd"
    ADAMW = "adamw"
    MUON = "muon"
    ADAM_MINI = "adam-mini"


@final
class SchedulerType(StrEnum):
    NONE = "none"
    COS_ANNEALING = "cosine_annealing"


@final
class ModelType(StrEnum):
    GTNO = "GTNO"


@final
class NormType(StrEnum):
    LAYER = "layer"
    RMS = "rms"


@final
class ValueResidualType(StrEnum):
    NONE = "none"
    LEARNABLE = "learnable"
    FIXED = "fixed"


@final
class GraphHeterogenousAttentionType(StrEnum):
    GHCA = "GHCA"


@final
class FFNActivation(StrEnum):
    RELU = "relu"
    RELU2 = "relu2"
    GELU = "gelu"
    SILU = "silu"
    SWIGLU = "swiglu"
