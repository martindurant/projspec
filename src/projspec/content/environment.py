from dataclasses import dataclass
from enum import auto

from projspec.utils import Enum
from projspec.content import BaseContent


class Stack(Enum):
    """The type of environment"""
    PIP = auto()
    CONDA = auto()


class Precision(Enum):
    # TODO: categories may be refined
    SPEC = auto()
    LOCK = auto()


@dataclass
class Environment(BaseContent):
    """Definition of a python runtime environment"""
    stack: Stack
    precision: Precision
    packages: list[str]
