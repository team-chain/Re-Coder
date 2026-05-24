/**
 * Context Collector - 활성 파일, 워크스페이스 정보 수집
 * (설계서 v6.4 기준)
 */
import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';

export interface WorkspaceContext {
    workspace_path: string;
    active_file_path: string;
    selected_text: string;
    project_files_summary: string;   // 주요 파일 목록 (최대 20개)
}

export class ContextCollector {
    collect(): WorkspaceContext {
        const editor = vscode.window.activeTextEditor;
        const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '';
        return {
            workspace_path: workspacePath,
            active_file_path: editor?.document.uri.fsPath ?? '',
            selected_text: editor?.document.getText(editor.selection) ?? '',
            project_files_summary: this._summarizeFiles(workspacePath),
        };
    }

    private _summarizeFiles(workspacePath: string): string {
        if (!workspacePath) return '';
        try {
            const important = ['package.json', 'requirements.txt', 'Dockerfile',
                               'docker-compose.yml', 'pyproject.toml', 'Pipfile',
                               'main.py', 'app.py', 'index.js', 'index.ts'];
            const found = important.filter(f => fs.existsSync(path.join(workspacePath, f)));
            return found.join(', ');
        } catch { return ''; }
    }
}
