import argparse


def parse_train_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--algorithm', '-a', 
                        type=str,
                        nargs='*')
    parser.add_argument('--environment', '-e',
                        type=str,
                        nargs='*',
                        default=[''])
    parser.add_argument('--directory', '-d',
                        type=str,
                        default='',
                        help='directory where checkpoints and "config.yaml" exist')
    parser.add_argument('--kwargs', '-kw',
                        type=str,
                        nargs='*',
                        default=[],
                        help="key-values in config.yaml needed to be overwrite")
    parser.add_argument('--trials', '-t',
                        type=int,
                        default=1,
                        help='number of trials')
    """ Arguments for logdir """
    parser.add_argument('--prefix', '-p',
                        default='',
                        help='directory prefix')
    parser.add_argument('--model-name', '-n',
                        default='',
                        help='model name')
    parser.add_argument('--logdir', '-ld',
                        type=str,
                        default='logs',
                        help='the logging directory. By default, all training data will be stored in logdir/env/algo/model_name')
    parser.add_argument('--grid-search', '-gs',
                        action='store_true')
    parser.add_argument('--delay',
                        default=1,
                        type=int)
    parser.add_argument('--verbose', '-v',
                        type=str,
                        default='warning',
                        help="the verbose level for python's built-in logging")
    parser.add_argument('--gpu',
                        type=int,
                        default=None,
                        nargs='*')
    args = parser.parse_args()

    return args


def parse_eval_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('directory',
                        type=str,
                        help='directory where checkpoints and "config.yaml" exist',
                        nargs='*')
    parser.add_argument('--record', '-r', 
                        action='store_true')
    parser.add_argument('--video_len', '-vl', 
                        type=int, 
                        default=None)
    parser.add_argument('--n_episodes', '-n', 
                        type=int, 
                        default=1)
    parser.add_argument('--n_envs', '-ne', 
                        type=int, 
                        default=0)
    parser.add_argument('--n_workers', '-nw', 
                        type=int, 
                        default=0)
    parser.add_argument('--size', '-s', 
                        nargs='+', 
                        type=int, 
                        default=None)
    parser.add_argument('--save', 
                        action='store_true')
    parser.add_argument('--fps', 
                        type=int, 
                        default=30)
    parser.add_argument('--verbose', '-v', 
                        type=str, 
                        default='warning')
    args = parser.parse_args()

    return args
