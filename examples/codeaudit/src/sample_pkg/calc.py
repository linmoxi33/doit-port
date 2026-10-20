"""Small arithmetic helpers."""

import os
import subprocess


def add(a, b):
    return a + b


def divide(a, b):
    return a / b


def run_shell(cmd):
    return subprocess.call(cmd, shell=True)


def read_env(name):
    return os.environ[name]


def swallow():
    try:
        raise ValueError("gone")
    except Exception:
        pass

