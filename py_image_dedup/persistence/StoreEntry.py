class StoreEntry:
    """
    Entry stored in an ImageSignatureStore

    TODO: is this a good idea?
    """

    def __init__(self,
                 path: str):
        self._path = path
