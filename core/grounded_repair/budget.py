"""Persisted worst-case reservation before each paid generation, including crashes."""
import json
import math
import sqlite3
import uuid
from pathlib import Path


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    def __init__(self, path:Path, limit:float):
        if not 0 < limit <= 1000:
            raise ValueError("Provide a positive experiment budget in USD")
        path.parent.mkdir(parents=True,exist_ok=True)
        self.path,self.limit=path,limit
        with sqlite3.connect(path) as db:
            db.execute('CREATE TABLE IF NOT EXISTS reservations (id TEXT PRIMARY KEY, reserved REAL, actual REAL, status TEXT)')

    def reserve(self, amount:float)->str:
        if not math.isfinite(amount) or amount < 0:
            raise ValueError('Reservation must be finite and non-negative')
        with sqlite3.connect(self.path,timeout=30) as db:
            db.execute('BEGIN IMMEDIATE')
            used=db.execute('SELECT COALESCE(SUM(COALESCE(actual,reserved)),0) FROM reservations').fetchone()[0]
            if used+amount>self.limit:
                raise BudgetExceeded(f'Budget ceiling ${self.limit:.2f}; reserved/used ${used:.4f}')
            key=str(uuid.uuid4())
            db.execute('INSERT INTO reservations VALUES (?,?,NULL,?)',(key,amount,'pending'))
            return key

    def settle(self,key:str,amount:float|None):
        if amount is not None and (not math.isfinite(amount) or amount < 0):
            raise ValueError('Measured cost must be finite and non-negative')
        with sqlite3.connect(self.path) as db:
            db.execute('UPDATE reservations SET actual=?,status=? WHERE id=?',(amount,'measured' if amount is not None else 'unknown',key))

    def summary(self):
        with sqlite3.connect(self.path) as db:
            row=db.execute('SELECT COUNT(*),COALESCE(SUM(actual),0),COALESCE(SUM(COALESCE(actual,reserved)),0) FROM reservations').fetchone()
        return {'limit_usd':self.limit,'calls_reserved':row[0],'measured_usd':row[1],'worst_case_used_usd':row[2]}


class BudgetedRouter:
    def __init__(self,router,budget:Budget,prices:dict):
        if any(not math.isfinite(float(r[k])) or float(r[k]) < 0 for r in prices.values() for k in ('input','output')):
            raise ValueError('Prices must be finite and non-negative')
        self.router,self.budget,self.prices=router,budget,prices

    def repair_profile(self):
        return self.router.repair_profile()

    async def call_repair(self,request,*,tier,run_id):
        profile=self.repair_profile()
        model=profile[tier]
        rate=self.prices[model]
        # UTF-8 byte count upper-bounds tokenizer input for these text models;
        # include schema, system and conservative message-format overhead.
        incoming=len((request.prompt+request.system+json.dumps(request.json_schema)).encode())+4096
        maximum=(incoming*rate['input']+request.max_tokens*rate['output'])/1_000_000
        key=self.budget.reserve(maximum)
        try:
            response=await self.router.call_repair(request,tier=tier,run_id=run_id)
            self.budget.settle(key,response.metadata['llm_call_record'].get('estimated_cost_usd'))
            return response
        except BaseException:
            # A timeout or cancellation can still be billed. Keep its reservation.
            self.budget.settle(key,None)
            raise
