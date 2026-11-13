// TwinFix 孪生环境部署任务（docs/06 §4）。任务名必须以 twinfix-twin- 开头：TwinFix 的 Jenkins 凭据只允许触发这类任务。
// 在孪生宿主机的 agent 上运行；该宿主机不得与生产网络互通。
pipeline {
    agent { label 'twin-host' }
    options {
        timeout(time: 30, unit: 'MINUTES')
        disableConcurrentBuilds(abortPrevious: false)
        buildDiscarder(logRotator(numToKeepStr: '50'))
    }
    parameters {
        string(name: 'REPO', description: '仓库 clone 地址')
        string(name: 'SHA', description: '要部署的提交（fix/twinfix-* 分支上的 sha 或 base_commit）')
        string(name: 'TWIN_ID', description: 'compose 项目名，形如 twinfix-<短id>')
        string(name: 'PORT_BASE', description: '端口段起点；order = PORT_BASE，inventory = PORT_BASE+1')
        string(name: 'DATA_SNAPSHOT', defaultValue: '', description: '数据快照名（空 = 种子数据）')
        string(name: 'CALLBACK_URL', defaultValue: '', description: '完成后回调 TwinFix 的地址')
        password(name: 'CALLBACK_TOKEN', defaultValue: '', description: '回调鉴权令牌')
    }
    environment {
        COMPOSE_PROJECT_NAME = "${params.TWIN_ID}"
        CTX = "${env.WORKSPACE}/ctx"
    }
    stages {
        stage('Validate') {
            steps {
                script {
                    if (!(params.TWIN_ID ==~ /^twinfix-[a-z0-9]{6,16}$/)) { error "invalid TWIN_ID ${params.TWIN_ID}" }
                    if (!(params.SHA ==~ /^[0-9a-f]{7,40}$/)) { error "invalid SHA ${params.SHA}" }
                    if (!(params.PORT_BASE ==~ /^[0-9]{4,5}$/)) { error "invalid PORT_BASE ${params.PORT_BASE}" }
                }
            }
        }
        stage('Checkout') {
            steps {
                sh '''
                    set -eu
                    rm -rf "$CTX" && mkdir -p "$CTX/src"
                    git -c core.hooksPath=/dev/null clone --quiet --no-checkout "$REPO" "$CTX/repo"
                    git -C "$CTX/repo" -c core.hooksPath=/dev/null archive --format=tar "$SHA" | tar -x -C "$CTX/src"
                    # 孪生模板（compose + Dockerfile + Maven 镜像配置）随 TwinFix 发布，放在 agent 的固定目录
                    cp /opt/twinfix/arena/twin/* "$CTX/"
                    cp /opt/twinfix/deploy/maven/settings.xml "$CTX/"
                '''
            }
        }
        stage('Deploy') {
            steps {
                sh '''
                    set -eu
                    export BUILD_CONTEXT="$CTX" PORT_ORDER="$PORT_BASE" PORT_INVENTORY=$((PORT_BASE + 1))
                    docker compose -f "$CTX/compose.twin.yml" -p "$TWIN_ID" up -d --build --wait --wait-timeout 600
                '''
            }
        }
        stage('Health') {
            steps {
                sh '''
                    set -eu
                    for port in "$PORT_BASE" $((PORT_BASE + 1)); do
                        curl -fsS --retry 30 --retry-delay 2 --retry-all-errors "http://127.0.0.1:$port/actuator/health" >/dev/null
                    done
                '''
            }
        }
    }
    post {
        always {
            script {
                if (params.CALLBACK_URL) {
                    def status = currentBuild.currentResult == 'SUCCESS' ? 'ready' : 'failed'
                    withEnv(["TWIN_STATUS=${status}"]) {
                        sh '''
                            curl -fsS -X POST "$CALLBACK_URL" \
                              -H "Authorization: Bearer $CALLBACK_TOKEN" -H 'Content-Type: application/json' \
                              -d "{\\"twin\\":\\"$TWIN_ID\\",\\"sha\\":\\"$SHA\\",\\"status\\":\\"$TWIN_STATUS\\",\\"build\\":\\"$BUILD_URL\\"}" || true
                        '''
                    }
                }
            }
        }
        failure {
            sh 'docker compose -p "$TWIN_ID" logs --tail 200 || true'
        }
    }
}
