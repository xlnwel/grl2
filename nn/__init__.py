import os

from nn.registry import *
from tools.file import load_files


def load_nn():
    load_files(os.path.dirname(os.path.realpath(__file__)))
    
load_nn()
