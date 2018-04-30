from setuptools import setup, find_packages

setup(
    name='py-image-dedup',
    version='0.0.1',
    description='A library to find duplicate images and delete unwanted ones',
    license='GPLv3+',
    author='Markus Ressel',
    author_email='mail@markusressel.de',
    url='https://www.markusressel.de',
    packages=find_packages(),
    classifiers=[
        'Development Status :: 4 - Beta',
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Programming Language :: Python :: 3.6',
    ],
    install_requires=[
        'setuptools',
        'scipy',
        'numpy',
        'image_match',
        'elasticsearch',
        'tqdm'
    ],
    tests_require=[

    ]
)
