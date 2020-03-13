from skbuild import setup
from setuptools import find_packages
import subprocess

# declare defaults
IS_RASPI = False
SCRIPTS = []
PACKAGES = []
CMAKE_ARGS = []
CMAKE_INSTALL_DIR = ''

# detect if on raspberry pi
ret = subprocess.call(['grep', '-q', 'BCM', '/proc/cpuinfo'])
if ret == 0:
    IS_RASPI = True


# configure for raspberry pi
if IS_RASPI:
    CMAKE_ARGS = ['-DPIGPIO=ON']
    #CMAKE_INSTALL_DIR = '/usr/local'
    SCRIPTS.append('autopilot/external/pigpio/pigpiod')
    PACKAGES.append('autopilot.external.pigpio')

setup(
    name="autopilot",
    version="0.3.0",
    description="Distributed behavioral experiments",
    author="Jonny Saunders",
    license="MPL2",
    scripts = SCRIPTS,
    # dependency_links=['src/pigpio/'],
    packages=find_packages().extend(PACKAGES),
    cmake_args=CMAKE_ARGS,
    cmake_install_dir = CMAKE_INSTALL_DIR,


)