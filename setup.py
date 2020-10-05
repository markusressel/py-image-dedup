import subprocess

from setuptools import setup, find_packages

VERSION_NUMBER = "1.0.0"

GIT_BRANCH = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"])
GIT_BRANCH = GIT_BRANCH.decode()  # convert to standard string
GIT_BRANCH = GIT_BRANCH.rstrip()  # remove unnecessary whitespace

if GIT_BRANCH == "master":
    DEVELOPMENT_STATUS = "Development Status :: 5 - Production/Stable"
    VERSION_NAME = VERSION_NUMBER
elif GIT_BRANCH == "beta":
    DEVELOPMENT_STATUS = "Development Status :: 4 - Beta"
    VERSION_NAME = "%s-beta" % VERSION_NUMBER
elif GIT_BRANCH == "dev":
    DEVELOPMENT_STATUS = "Development Status :: 3 - Alpha"
    VERSION_NAME = "%s-dev" % VERSION_NUMBER
else:
    print("Unknown git branch, using pre-alpha as default")
    DEVELOPMENT_STATUS = "Development Status :: 2 - Pre-Alpha"
    VERSION_NAME = "%s-%s" % (VERSION_NUMBER, GIT_BRANCH)


def readme_type() -> str:
    import os
    if os.path.exists("README.rst"):
        return "text/x-rst"
    if os.path.exists("README.md"):
        return "text/markdown"


def readme() -> [str]:
    with open('README.md') as f:
        return f.read()


def locked_requirements(section):
    """
    Look through the 'Pipfile.lock' to fetch requirements by section.
    """
    import json

    with open('Pipfile.lock') as pip_file:
        pipfile_json = json.load(pip_file)

    if section not in pipfile_json:
        print("{0} section missing from Pipfile.lock".format(section))
        return []

    return [package + detail.get('version', "")
            for package, detail in pipfile_json[section].items()]


setup(
    name='py-image-dedup',
    version=VERSION_NAME,
    description='A library to find duplicate images and delete unwanted ones',
    long_description=readme(),
    long_description_content_type=readme_type(),
    license='GPLv3+',
    author='Markus Ressel',
    author_email='mail@markusressel.de',
    url='https://github.com/markusressel/py-image-dedup',
    packages=find_packages(),
    # python_requires='>=3.4',
    classifiers=[
        DEVELOPMENT_STATUS,
        'License :: OSI Approved :: GNU General Public License v3 or later (GPLv3+)',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7'
        'Programming Language :: Python :: 3.8'
    ],
    install_requires=locked_requirements('default'),
    tests_require=locked_requirements('develop'),
    entry_points={
        'console_scripts': [
            'py-image-dedup = py_image_dedup.cli:cli'
        ]
    }
)
