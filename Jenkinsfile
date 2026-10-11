// Minimal CI/CD pipeline: test, build the existing Dockerfile, deploy a temporary
// container, verify /health, then always clean up.
//
// Docker commands run against the TLS Docker-in-Docker sidecar defined in
// jenkins/docker-compose.yml (DOCKER_HOST is set on the Jenkins container).
// No credentials are used or required. Nothing here touches AWS or the EC2 deployment.
pipeline {
    agent any

    options {
        // Only the explicit Checkout stage below fetches source.
        skipDefaultCheckout(true)
        // One build at a time, because the deployment port is fixed.
        disableConcurrentBuilds()
        timeout(time: 15, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    environment {
        IMAGE_NAME     = 'sre-platform'
        IMAGE_TAG      = "ci-${BUILD_NUMBER}"
        CONTAINER_NAME = "sre-platform-ci-${BUILD_NUMBER}"
        // Isolated from the 8000 service port, the 18000 manual demo port, and Jenkins on 8080.
        DEPLOY_PORT    = '18100'
        // The sidecar's hostname on the private compose network. It publishes the
        // container port, so Jenkins reaches it here rather than at localhost.
        DEPLOY_HOST    = 'docker'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
            }
        }

        stage('Install Dependencies') {
            steps {
                // ".venv" is already excluded by .dockerignore, so it stays out of the image build context.
                sh '''
                    python3 -m venv .venv
                    .venv/bin/python -m pip install --quiet --no-cache-dir -r requirements.txt
                '''
            }
        }

        stage('Test') {
            steps {
                // Same command as local development; a failing test fails the build.
                sh '''
                    PATH="$PWD/.venv/bin:$PATH"
                    python -m unittest discover -s tests -v
                '''
            }
        }

        stage('Build Image') {
            steps {
                sh 'docker build -t "$IMAGE_NAME:$IMAGE_TAG" .'
            }
        }

        stage('Deploy Temporary Container') {
            steps {
                sh '''
                    docker run -d \
                        --name "$CONTAINER_NAME" \
                        -p "$DEPLOY_PORT:8000" \
                        "$IMAGE_NAME:$IMAGE_TAG"
                '''
            }
        }

        stage('Verify Health') {
            steps {
                sh '''
                    echo "Waiting for the container HEALTHCHECK..."
                    status=starting
                    for i in $(seq 1 30); do
                        status=$(docker inspect --format '{{.State.Health.Status}}' "$CONTAINER_NAME")
                        echo "health status: $status"
                        if [ "$status" = healthy ] || [ "$status" = unhealthy ]; then
                            break
                        fi
                        sleep 2
                    done
                    if [ "$status" != healthy ]; then
                        echo "Container did not become healthy (last status: $status)"
                        exit 1
                    fi

                    url="http://${DEPLOY_HOST}:${DEPLOY_PORT}/health"
                    code=$(curl -sS -o health-body.txt -w '%{http_code}' --max-time 5 "$url")
                    echo "GET $url -> HTTP $code"
                    cat health-body.txt
                    echo
                    if [ "$code" != 200 ]; then
                        echo "Expected HTTP 200"
                        exit 1
                    fi
                    grep -q '"status":"healthy"' health-body.txt
                '''
            }
        }
    }

    post {
        // Runs whether the build succeeded, failed, or was aborted after a node was allocated.
        always {
            sh '''
                echo "--- container logs (last 20 lines) ---"
                docker logs --tail 20 "$CONTAINER_NAME" 2>&1 || true
                echo "--- cleanup ---"
                docker rm -f "$CONTAINER_NAME" || true
                docker rmi -f "$IMAGE_NAME:$IMAGE_TAG" || true
                echo "Remaining containers matching $CONTAINER_NAME:"
                docker ps -a --filter "name=$CONTAINER_NAME" --format '{{.Names}}'
                echo "(end of list)"
            '''
        }
    }
}
