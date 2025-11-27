def pad(i: int, p: int):
    """Pad node names so they all are the same length."""
    return f"{i}".zfill(len(str(p)))
