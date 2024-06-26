from game.fsp import FSP
from game.pfsp import PFSP

def select_sampling_strategy(type, **kwargs):
  candidate_strategies = dict(
    fsp=FSP, 
    pfsp=PFSP, 
  )
  return candidate_strategies[type](**kwargs)
