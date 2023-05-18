import importlib

from core.log import do_logging


def pkg_str(root_dir, separator, base_name=None):
    if base_name is None:
        return root_dir
    return f'{root_dir}{separator}{base_name}'


def get_package_from_algo(algo, place=0, separator='.'):
    algo = algo.split('-', 1)[place]

    pkg = get_package('algo', algo, separator)
    if pkg is None:
        pkg = get_package('distributed', algo, separator)

    return pkg


def get_package(root_dir, base_name=None, separator='.', backtrack=3):
    for i in range(1, 10):
        indexed_root_dir = root_dir if i == 1 else f'{root_dir}{i}'
        pkg = pkg_str(indexed_root_dir, '.', base_name)
        try:
            if importlib.util.find_spec(pkg) is not None:
                pkg = pkg_str(indexed_root_dir, separator, base_name)
                return pkg
        except Exception as e:
            do_logging(f'{e}', backtrack=backtrack)
            return None
    return None


def import_module(name, pkg=None, algo=None, *, config=None, place=0):
    """ import <name> module from <pkg>, 
    if <pkg> is not provided, import <name> module
    according to <algo> or "algorithm" in <config> 
    """
    if pkg is None:
        algo = algo or config['algorithm']
        assert isinstance(algo, str), algo
        pkg = get_package_from_algo(algo=algo, place=place)
        m = importlib.import_module(f'{pkg}.{name}')
    else:
        pkg = get_package(root_dir=pkg, base_name=name, backtrack=4)
        m = importlib.import_module(pkg)

    return m


def import_main(module, algo=None, *, config=None):
    algo = algo or config['algorithm']
    if '-' in algo:
        module = '.'.join([algo.split('-')[0], module])
    assert isinstance(algo, str), algo
    if '-' in algo:
        m = importlib.import_module(f'distributed.{module}')
    else:
        place = 0 if module.startswith('train') else -1
        pkg = get_package_from_algo(algo, place=place)
        m = importlib.import_module(f'{pkg}.{module}')

    return m.main
