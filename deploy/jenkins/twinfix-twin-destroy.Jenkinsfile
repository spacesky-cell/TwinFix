// TwinFix 孪生环境销毁任务：合并、取消或 TTL 到期后由 TwinFix 触发。
pipeline {
    agent { label 'twin-host' }
    options { timeout(time: 10, unit: 'MINUTES') }
    parameters {
        string(name: 'TWIN_ID', description: 'compose 项目名，形如 twinfix-<短id>')
    }
    stages {
        stage('Destroy') {
            steps {
                script {
                    if (!(params.TWIN_ID ==~ /^twinfix-[a-z0-9]{6,16}$/)) { error "invalid TWIN_ID ${params.TWIN_ID}" }
                }
                sh 'docker compose -p "$TWIN_ID" down -v --remove-orphans --rmi local'
            }
        }
    }
}
