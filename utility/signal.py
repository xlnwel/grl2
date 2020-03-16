import signal
import sys
import ray


def sigint_shutdown_ray():
    """ Shutdown ray when the process is terminated by ctrl+C """
    def handler(sig, frame):
        if ray.is_initialized():
            ray.shutdown()
            print('ray has been shutdown by sigint')
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)
