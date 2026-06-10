import { execSync } from 'child_process';
import { writeFileSync } from 'fs';

// Try wsl with output redirection to a Windows file
const r = execSync('wsl.exe -d Ubuntu-22.04 -- bash -c "echo HELLO_FROM_WSL > /mnt/e/codex/tmp_wsl_test.txt 2>&1"', { encoding: 'utf8', timeout: 10000 });
console.log('done');