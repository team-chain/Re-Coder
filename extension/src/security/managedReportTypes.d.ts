export type ManagedMode = 'audit' | 'redteam' | 'scans';
export interface ManagedFinding {title:string;severity:string;file?:string;line?:number;fix?:string}
export interface ManagedCheck {tool:string;status:'completed'|'failed'|'not_applicable';message?:string;count?:number;blockingCount?:number;findings:ManagedFinding[]}
export interface ManagedReport {
    schema:1;runId:string;sourceSha256:string;mode:ManagedMode;status:string;stage:string;
    error?:string;cleaned?:boolean;isolationVerified?:boolean;
    tasks:Array<{id:string;title:string;status:string;result:string}>;
    evidence?:Array<{id:string;type:'report'|'done'|'ask';message:string;result:string}>;
    assessment?:import('./pentagiAssessmentTypes').PentagiAssessment;
    scans?:{checks:ManagedCheck[];policy?:{decision:'allow'|'deny';reason:string;scope:string}};
    audit?:{scope:string;files:string[];skipped:string[];calls:number;inputTokens:number;outputTokens:number;
        findings:Array<ManagedFinding&{file:string;line:number;impact:string;verification:string}>;
        needsValidation:Array<{title:string;reason:string}>};
}
