export type CheckKind='discovery'|'sql_injection'|'authentication'|'authorization'|'input_handling';
export interface AssessmentCheck {kind:CheckKind;status:'tested'|'blocked'|'not_tested';reason:string;requests:string[]}
export interface AssessmentFinding {
 title:string;kind:CheckKind;route:string;observation:string;remediation:string;
 request:string;response:string;controlRequest:string;controlResponse:string;
 evidence:'agent_reported';cvss?:{vector:string;score:number};
}
export interface PentagiAssessment {schema:1;status:'review_required'|'incomplete';checks:AssessmentCheck[];findings:AssessmentFinding[];warnings:string[];limitations:string[]}
