"""Runs the demucs command line with every `torch.load` forced to
`weights_only=True`.

A demucs checkpoint is a pickle that names the model class and a few plain
values. With `weights_only` the unpickler refuses any global that was not
allow-listed, so a checkpoint that had been tampered with cannot run code while it
is loaded. The allow-list below is exactly the set of globals the pinned weights
contain (listed with `pickletools`), and nothing more.

    python -m localization.providers.demucs_safe --two-stems=vocals -o OUT -- input.wav
"""

import sys


def main() -> None:
    import codecs
    from fractions import Fraction

    import numpy
    import torch
    from demucs.htdemucs import HTDemucs

    # The checkpoint was pickled by numpy 1, which named this function
    # `numpy.core.multiarray.scalar`. numpy 2 moved it to `numpy._core`, and the
    # allow-list matches on the name written in the pickle, so register the same
    # function under the old name.
    multiarray = getattr(numpy, "_core", None) or numpy.core
    scalar = multiarray.multiarray.scalar

    def numpy_scalar(*args):  # noqa: ANN002, ANN202
        return scalar(*args)

    numpy_scalar.__module__ = "numpy.core.multiarray"
    numpy_scalar.__name__ = "scalar"

    torch.serialization.add_safe_globals(
        [
            HTDemucs,
            Fraction,
            numpy.dtype,
            numpy.dtypes.Float64DType,
            numpy_scalar,
            codecs.encode,
        ]
    )
    original = torch.load

    def load_weights_only(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        kwargs["weights_only"] = True
        return original(*args, **kwargs)

    torch.load = load_weights_only
    from demucs.separate import main as demucs_main

    demucs_main()


if __name__ == "__main__":
    sys.exit(main())
