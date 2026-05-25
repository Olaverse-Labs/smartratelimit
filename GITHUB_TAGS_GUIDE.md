# GitHub Tags Guide

## Two Types of "Tags" on GitHub

### 1. Repository Topics (Keywords) - For Discovery

**Where to add:** On your repository homepage (NOT in Settings → Rulesets)

**Steps:**
1. Go to your repository: https://github.com/olastephen/smartratelimit
2. Click the **gear icon (⚙️)** next to "About" section (top right of repository)
3. In the "Topics" field, add these keywords:
   ```
   python
   rate-limiting
   api
   429-error
   throttling
   requests
   httpx
   aiohttp
   python-library
   api-management
   ```
4. Click "Save changes"

**Note:** If you're being redirected to "Rulesets", you're in the wrong place. Topics are added on the repository homepage, not in Settings.

---

### 2. Git Tags (For Releases)

**Option A: Create via Git (Recommended)**

```bash
# Create annotated tag
git tag -a v0.3.0 -m "v0.3.0 - Production Ready"

# Push tag to GitHub
git push origin v0.3.0

# Or push all tags
git push --tags
```

**Option B: Create via GitHub UI**

1. Go to: https://github.com/olastephen/smartratelimit/releases
2. Click "Create a new release"
3. Choose tag: `v0.3.0` (create new tag)
4. Release title: "v0.3.0 - Production Ready"
5. Description: Copy from `CHANGELOG.md`
6. Click "Publish release"

---

## Troubleshooting

### If GitHub redirects you to "Rulesets":

- **For Topics:** You're in the wrong place. Go to the repository homepage (not Settings) and click the gear icon next to "About"
- **For Git Tags:** You might have branch protection rules. Use the Releases page instead, or push tags via command line

### If you can't push tags:

```bash
# Check if you have write access
git push origin v0.3.0

# If it fails, try creating via GitHub Releases page instead
```

---

## Quick Commands

```bash
# List all tags
git tag -l

# Create and push tag
git tag -a v0.3.0 -m "v0.3.0 - Production Ready"
git push origin v0.3.0

# Delete a tag (if needed)
git tag -d v0.3.0
git push origin --delete v0.3.0
```

