import { parentPort, workerData } from 'worker_threads';
import { analyzeFile, analyzeProject } from './analyzer';
import * as fs from 'fs';

try {
    if (!workerData.file) fs.readdirSync(workerData.workspace);
    const graph = workerData.file ? analyzeFile(workerData.file) : analyzeProject(workerData.workspace);
    parentPort?.postMessage({ graph });
} catch (error) {
    parentPort?.postMessage({ error: error instanceof Error ? error.message : String(error) });
}
