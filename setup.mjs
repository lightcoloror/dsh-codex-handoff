#!/usr/bin/env node
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const result=spawnSync(process.env.PYTHON || 'python',['-X','utf8','-B',fileURLToPath(new URL('./setup.py',import.meta.url)),...process.argv.slice(2)],{stdio:'inherit',windowsHide:true});
if(result.error){console.error('Python 3.11+ is required. Set PYTHON to its executable path.');process.exitCode=1;}
else process.exitCode=result.status ?? 1;
