import setuptools

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setuptools.setup(
    name="iNTUition",
    version="0.3.0",
    description="iNTUition learning operations dashboard and CLI",
    long_description=long_description,
    long_description_content_type="text/markdown",
    packages=setuptools.find_packages(),
    # The iNTUition dashboard page lives on disk rather than in a string literal.
    package_data={"intuition": [
        "static/*.html",
        "static/vendor/katex/*.css", "static/vendor/katex/*.js",
        "static/vendor/katex/LICENSE",
        "static/vendor/katex/contrib/*.js",
        "static/vendor/katex/fonts/*",
        "static/vendor/three/*.js", "static/vendor/three/LICENSE",
        "static/vendor/three/jsm/controls/*.js",
        "static/vendor/codemirror/**/*.mjs",
    ]},
    include_package_data=True,
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.7",
    install_requires=["beautifulsoup4>=4.7.1", "requests>=2.22.0", "lxml>=4.5.1"],
    # Drive support is optional; the rest of the tool runs without it.
    extras_require={
        "drive": ["google-api-python-client>=2.0", "google-auth-oauthlib>=1.0",
                  "pypdf>=4.0", "pymupdf>=1.24"],
        # schedule_import reads a STARS PDF through pypdf's layout extraction, so the
        # timetable import needs it even when Drive is not configured.
        "schedule": ["pypdf>=4.0"],
        "transcribe": ["faster-whisper>=1.0"],
        "research": ["anthropic>=0.40"],
        "inbound": ["playwright>=1.45"],
        "desktop": ["pywebview>=5.0", "pyinstaller>=6.0"],
    },
    entry_points={
        "console_scripts": [
            "intuition-desktop=intuition.desktop:main",
        ],
    },
)

