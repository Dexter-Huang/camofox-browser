/** Re-export process spawning so route modules stay free of child_process. */
import { spawn as childSpawn } from 'node:child_process';

export const spawn = childSpawn;
