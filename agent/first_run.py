# first_run.py
# 프로그램 첫 실행 시 또는 USER_TOKEN 만료 시 자동 팝업됩니다.
# 탭 1: 서버 로그인 (이메일/비밀번호 -> POST /auth/login -> token + user_id)
# 탭 2: Gemini API 키 (로컬 .env에만 저장, 서버 전송 없음)

import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import requests

API_BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8000')


def show_setup_window():
    win = tk.Tk()
    win.title('AI 업무 어시스턴트 - 초기 설정')
    win.geometry('520x420')
    win.resizable(False, False)

    header = tk.Frame(win, bg='#1E3A5F', height=60)
    header.pack(fill='x')
    tk.Label(
        header,
        text='AI 업무 어시스턴트',
        font=('Arial', 14, 'bold'),
        bg='#1E3A5F',
        fg='white'
    ).pack(pady=15)

    notebook = ttk.Notebook(win)
    notebook.pack(fill='both', expand=True, padx=15, pady=10)

    # 탭 1: 서버 로그인
    tab1 = tk.Frame(notebook, padx=20, pady=15)
    notebook.add(tab1, text='  1. 서버 로그인  ')

    tk.Label(tab1, text='이메일', anchor='w', font=('Arial', 10)).pack(fill='x', pady=(8, 2))
    email_entry = tk.Entry(tab1, width=45, font=('Arial', 10))
    email_entry.pack(fill='x', ipady=4)

    tk.Label(tab1, text='비밀번호', anchor='w', font=('Arial', 10)).pack(fill='x', pady=(10, 2))
    pw_entry = tk.Entry(tab1, width=45, show='*', font=('Arial', 10))
    pw_entry.pack(fill='x', ipady=4)

    login_status = tk.Label(tab1, text='', fg='red', font=('Arial', 9), wraplength=420)
    login_status.pack(pady=8)

    tk.Label(
        tab1,
        text='계정이 없으신가요? 먼저 회원가입 후 로그인하세요.',
        fg='gray',
        font=('Arial', 9)
    ).pack()

    # 탭 2: Gemini API 키
    tab2 = tk.Frame(notebook, padx=20, pady=15)
    notebook.add(tab2, text='  2. Gemini API 키  ')

    tk.Label(
        tab2,
        text='Gemini API 키를 입력해주세요',
        font=('Arial', 13, 'bold')
    ).pack(pady=(10, 4))

    tk.Label(
        tab2,
        text='키 발급: aistudio.google.com/app/apikey',
        fg='gray',
        font=('Arial', 9)
    ).pack()

    gemini_entry = tk.Entry(tab2, width=50, show='*', font=('Arial', 10))
    gemini_entry.pack(pady=12, ipady=4)

    tk.Label(
        tab2,
        text='이 키는 로컬 .env에만 저장됩니다. 서버로 전송되지 않습니다.',
        fg='#E67E22',
        font=('Arial', 9),
        wraplength=420
    ).pack()

    def save():
        email = email_entry.get().strip()
        password = pw_entry.get().strip()
        gemini_key = gemini_entry.get().strip()

        if not email or not password:
            login_status.config(text='이메일과 비밀번호를 입력해주세요.')
            notebook.select(0)
            return

        if not gemini_key:
            messagebox.showwarning('경고', 'Gemini API 키를 입력해주세요.')
            notebook.select(1)
            return

        login_status.config(text='로그인 중...', fg='gray')
        win.update()

        try:
            resp = requests.post(
                f'{API_BASE_URL}/auth/login',
                json={'email': email, 'password': password},
                timeout=10
            )
            if resp.status_code == 401:
                login_status.config(text='로그인 실패: 이메일 또는 비밀번호가 틀렸습니다.', fg='red')
                notebook.select(0)
                return
            elif resp.status_code != 200:
                login_status.config(text='서버 오류: ' + str(resp.status_code), fg='red')
                notebook.select(0)
                return

            data = resp.json()
            token = data['token']
            user_id = data['user_id']

        except requests.exceptions.ConnectionError:
            login_status.config(text='서버에 연결할 수 없습니다. 백엔드 서버가 실행 중인지 확인하세요.', fg='red')
            notebook.select(0)
            return
        except Exception as e:
            login_status.config(text='서버 연결 실패: ' + str(e), fg='red')
            notebook.select(0)
            return

        env_content = (
            'GEMINI_API_KEY=' + gemini_key + '\n'
            'USER_TOKEN=' + token + '\n'
            'USER_ID=' + user_id + '\n'
            'API_BASE_URL=' + API_BASE_URL + '\n'
            'API_WS_URL=' + API_BASE_URL.replace('http://', 'ws://').replace('https://', 'wss://') + '\n'
        )
        Path('.env').write_text(env_content, encoding='utf-8')

        messagebox.showinfo('완료', '설정이 저장되었습니다. AI 업무 어시스턴트를 시작합니다!')
        win.destroy()

    btn_frame = tk.Frame(win)
    btn_frame.pack(pady=5)
    tk.Button(
        btn_frame,
        text='시작하기',
        command=save,
        bg='#1E3A5F',
        fg='white',
        font=('Arial', 11, 'bold'),
        width=22,
        height=2,
        relief='flat',
        cursor='hand2'
    ).pack()

    win.mainloop()


if __name__ == '__main__':
    show_setup_window()