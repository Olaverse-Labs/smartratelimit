# Publishing to PyPI Guide

## Prerequisites

1. **PyPI Account**: Create one at https://pypi.org/account/register/
2. **API Token**: Get from https://pypi.org/manage/account/token/
   - Create a new API token with "Entire account" scope
   - Save the token (starts with `pypi-`)

## Step-by-Step Process

### 1. Install Build Tools

```bash
pip install build twine
```

### 2. Clean Previous Builds

```bash
rm -rf dist/ build/ *.egg-info
```

### 3. Build the Package

```bash
python -m build
```

This creates:
- `dist/smartratelimit-0.3.0.tar.gz` (source distribution)
- `dist/smartratelimit-0.3.0-py3-none-any.whl` (wheel)

### 4. Check the Package (Optional but Recommended)

```bash
twine check dist/*
```

### 5. Test on TestPyPI First (Recommended)

```bash
# Upload to TestPyPI
twine upload --repository testpypi dist/*

# Test installation
pip install --index-url https://test.pypi.org/simple/ smartratelimit
```

### 6. Publish to Real PyPI

```bash
twine upload dist/*
```

You'll be prompted for:
- Username: `__token__`
- Password: Your PyPI API token (starts with `pypi-`)

### 7. Verify Installation

```bash
pip install smartratelimit
python -c "import smartratelimit; print(smartratelimit.__version__)"
```

## Quick Commands

```bash
# Full process
pip install build twine
rm -rf dist/ build/ *.egg-info
python -m build
twine check dist/*
twine upload dist/*
```

## Troubleshooting

### "Package already exists"
- Version already published. Increment version in `pyproject.toml` and `smartratelimit/__init__.py`

### "Invalid credentials"
- Make sure you're using `__token__` as username
- Use the full API token (starts with `pypi-`) as password

### "Package name taken"
- The name `smartratelimit` should be available, but if not, you'll need to choose a different name

## After Publishing

1. **Verify on PyPI**: https://pypi.org/project/smartratelimit/
2. **Update README**: Add PyPI badge
3. **Test installation**: `pip install smartratelimit`
4. **Update GitHub**: Add PyPI installation instructions

