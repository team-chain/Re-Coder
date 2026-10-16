import { parentPort, workerData } from 'worker_threads';
import { analyzeFile, analyzeProject } from './analyzer';
import { analyzeFlow, previewFlow } from './flow';
import * as fs from 'fs';

try {
    const mode: string = workerData.mode || (workerData.file ? 'file' : 'project');
    fs.readdirSync(workerData.workspace);
    let graph: unknown;
    if (mode === 'flow') { graph = analyzeFlow(workerData.workspace); }
    else if (mode === 'preview') { graph = previewFlow(workerData.workspace, new Map(workerData.overrides || [])); }
    else if (mode === 'file') { graph = analyzeFile(workerData.file); }
    else { graph = analyzeProject(workerData.workspace); }
    parentPort?.postMessage({ graph });
} catch (error) {
    parentPort?.postMessage({ error: error instanceof Error ? error.message : String(error) });
}
