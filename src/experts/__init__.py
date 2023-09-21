from .synthetic import *
from src.configs import SyntheticExpertConfig


def get_expert(conf: SyntheticExpertConfig) -> Expert:
    if conf.name == "synthetic":
        return SyntheticExpert(conf)
    else:
        raise NotImplementedError
