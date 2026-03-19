try:
    from antithesis.random import AntithesisRandom

    urand = AntithesisRandom()
except ImportError:
    from random import SystemRandom

    urand = SystemRandom()
randrange = urand.randrange
random = urand.random
sample = urand.sample
choice = urand.choice
choices = urand.choices
randint = urand.randint
shuffle = urand.shuffle
normalvariate = urand.normalvariate
gauss = urand.gauss
