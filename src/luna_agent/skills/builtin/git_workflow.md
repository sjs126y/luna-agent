# Git Workflow

Standard Git operations and best practices.

## Branching
```bash
# Create and switch to new branch
git checkout -b feature/my-feature

# Keep branch updated with main
git fetch origin
git rebase origin/main
```

## Commits
```bash
# Conventional commit format
git commit -m "feat: add user authentication"
git commit -m "fix: resolve login timeout issue"
git commit -m "refactor: extract validation logic"
git commit -m "docs: update API documentation"
git commit -m "test: add unit tests for auth module"
```

## Undoing
```bash
# Undo last commit (keep changes)
git reset --soft HEAD~1

# Discard uncommitted changes
git checkout -- filename

# Revert a pushed commit
git revert <commit-hash>
```

## Common Scenarios
```bash
# Squash last 3 commits
git rebase -i HEAD~3

# Stash work temporarily
git stash push -m "WIP: feature X"
git stash pop

# Cherry-pick a commit from another branch
git cherry-pick <commit-hash>
```
