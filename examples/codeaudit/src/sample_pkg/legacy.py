"""Legacy helpers."""

import pickle


def load(data):
    return pickle.loads(data)


def compare(a, b):
    if a == None:
        return False
    if a is b == True:
        return True
    return a == b
