/** Only read-only status requests may retry; never replay start, stop or approvals. */
export function redTeamPollDelay(action:string,failures:number):number|undefined {
 if(!['localStatus','status','managedStatus'].includes(action)||failures<1||failures>3)return;
 return 3000*2**(failures-1);
}
export const REDTEAM_STATUS_TIMEOUT=45000;
