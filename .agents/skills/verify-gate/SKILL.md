---
name: verify-gate
description: Runs the mandatory Windows PowerShell verification gate (ruff and pytest live combat tests). Use this before finishing any code modification or when the user asks to test/validate the project.
---

# Mandatory Verification Gate

## Instructions
1. Execute the verification suite directly via the canonical helper script:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .agents/skills/verify-gate/scripts/verify.ps1
   # Optional strict typecheck mode:
   # powershell -ExecutionPolicy Bypass -File .agents/skills/verify-gate/scripts/verify.ps1 -Strict
   ```
2. Helper script: [scripts/verify.ps1](./scripts/verify.ps1)
3. **Self-Correction Loop**: If ANY command fails, fix the root cause in code and re-run (up to 3 attempts) before reporting back to the user.
