# Shell Guide

Common shell commands and patterns for system administration.

## File Operations
```bash
# Find files by name
find /path -name "*.py" -type f

# Search file contents
grep -r "pattern" /path --include="*.py"

# Disk usage
du -sh /path/to/dir
df -h
```

## Process Management
```bash
# List processes
ps aux | grep process_name

# Kill process by name
pkill -f process_name

# Background process with nohup
nohup ./script.sh > output.log 2>&1 &
```

## Text Processing
```bash
# Count lines
wc -l file.txt

# Sort and deduplicate
sort file.txt | uniq

# Extract column
awk '{print $2}' file.txt

# Replace in-place
sed -i 's/old/new/g' file.txt
```

## Network
```bash
# Check port usage
netstat -tlnp | grep :8080
lsof -i :8080

# Test HTTP endpoint
curl -X POST http://localhost:8080/api -H "Content-Type: application/json" -d '{"key":"value"}'

# Download file
wget https://example.com/file.tar.gz
```

## Windows Equivalents
| Linux | Windows (PowerShell) |
|-------|---------------------|
| `grep` | `Select-String` |
| `find` | `Get-ChildItem -Recurse` |
| `curl` | `Invoke-WebRequest` |
| `ps` | `Get-Process` |
| `kill` | `Stop-Process` |
