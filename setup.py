# Copyright (c) 2015 Yubico AB
# All rights reserved.
#
#   Redistribution and use in source and binary forms, with or
#   without modification, are permitted provided that the following
#   conditions are met:
#
#    1. Redistributions of source code must retain the above copyright
#       notice, this list of conditions and the following disclaimer.
#    2. Redistributions in binary form must reproduce the above
#       copyright notice, this list of conditions and the following
#       disclaimer in the documentation and/or other materials provided
#       with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS
# FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE
# COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
# INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING,
# BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
# LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
# LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN
# ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

import sys
import os
from setuptools import setup, find_packages

install_requires = [
    "pyscard",
    "click",
    "cryptography",
    "pyopenssl",
    "dataclasses;python_version<'3.7'",
    # TODO: Replace below with "fido2 >=0.9, <1.0",
    "fido2 @ https://api.github.com/repos/Yubico/python-fido2/tarball/master",
]
if sys.platform == "win32":
    install_requires.append("pypiwin32")

with open(os.path.join(os.path.dirname(__file__), "ykman/VERSION")) as version_file:
    version = version_file.read().strip()

setup(
    name="yubikey-manager",
    version=version,
    author="Dain Nilsson",
    author_email="dain@yubico.com",
    maintainer="Yubico Open Source Maintainers",
    maintainer_email="ossmaint@yubico.com",
    url="https://github.com/Yubico/yubikey-manager",
    description="Tool for managing your YubiKey configuration.",
    license="BSD 2 clause",
    entry_points={"console_scripts": ["ykman=ykman.cli.__main__:main"]},
    packages=find_packages(exclude=["test", "test.*"]),
    install_requires=install_requires,
    package_data={"ykman": ["VERSION"]},
    include_package_data=True,
    classifiers=[
        "License :: OSI Approved :: BSD License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: End Users/Desktop",
        "Topic :: Security :: Cryptography",
        "Topic :: Utilities",
    ],
)
