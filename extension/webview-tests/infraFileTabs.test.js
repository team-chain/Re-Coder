const test = require("node:test");
const assert = require("node:assert/strict");

const {
  canRunSecurityScan,
  generationCommandForTab,
  infraFileLabelForTab,
  shouldRunDockerPipeline,
} = require("../out/webview-test/components/ShipMode.js");

test("각 인프라 탭은 대응하는 생성 명령과 파일명을 사용한다", () => {
  assert.deepEqual(
    ["dockerfile", "compose", "actions"].map((tab) => [
      generationCommandForTab(tab),
      infraFileLabelForTab(tab),
    ]),
    [
      ["generateDockerfile", "Dockerfile"],
      ["generateCompose", "docker-compose.yml"],
      ["generateGithubActions", ".github/workflows/deploy.yml"],
    ],
  );
});

test("Docker 배포 파이프라인은 Dockerfile 승인 뒤에만 실행한다", () => {
  assert.equal(shouldRunDockerPipeline("dockerfile"), true);
  assert.equal(shouldRunDockerPipeline("docker_compose"), false);
  assert.equal(shouldRunDockerPipeline("github_actions"), false);
});

test("파일 저장 응답을 기다리는 동안 보안 스캔을 실행하지 않는다", () => {
  assert.equal(canRunSecurityScan("preview"), true);
  assert.equal(canRunSecurityScan("saving"), false);
  assert.equal(canRunSecurityScan("scanning"), false);
  assert.equal(canRunSecurityScan("planReady"), false);
});
