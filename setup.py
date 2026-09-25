from setuptools import setup, Extension
from Cython.Build import cythonize

extensions = [
    Extension(
        "src.engine.matching_engine",
        ["src/engine/matching_engine.pyx"],
    )
]

setup(
    name="chronosmatch-engine",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": 3,
        },
    ),
)
