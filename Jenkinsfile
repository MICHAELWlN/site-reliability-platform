// CI/CD pipeline: test, build the existing Dockerfile, deploy a temporary container,
// verify /health, publish a linux/amd64 image to Amazon ECR, verify it there, then
// deploy that verified digest through the SREPlatformDeploy SSM document.
// Always cleans up. The deploy stage sends only the restricted SREPlatformDeploy document.
//
// Docker commands run against the TLS Docker-in-Docker sidecar defined in
// jenkins/docker-compose.yml (DOCKER_HOST is set on the Jenkins container).
//
// The first six stages need no credentials. The ECR stages use short-lived credentials
// for sre-jenkins-deploy-role that the operator loads with jenkins/aws-creds-handoff.sh.
// They live in a tmpfs, are read through the AWS CLI's credential_process, and are never
// in the environment, a command-line argument, or the job configuration. The pipeline
// removes them in post { always }.
pipeline {
    agent any

    options {
        // Only the explicit Checkout stage below fetches source.
        skipDefaultCheckout(true)
        // One build of this job at a time. The local health-check port is fixed, and
        // this job is the only pipeline that deploys to the EC2 instance. The host
        // script also holds a flock, so a second deploy exits 11 and fails the build.
        disableConcurrentBuilds()
        timeout(time: 25, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    parameters {
        // Only needed to reuse an image that already exists in ECR for this commit but has no
        // publish record, for example one pushed before records existed or by a build that
        // failed before writing one. Leave empty for normal builds.
        string(
            name: 'EXPECTED_IMAGE_DIGEST',
            defaultValue: '',
            description: 'Optional sha256 digest you have verified for this commit\'s existing ECR image. Used only if the tag already exists and no publish record exists. Never causes a push, overwrite or delete.'
        )
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

        // Publishing. The registry host is derived from the credentials at run time, so no
        // account ID is stored in Git. The image tag is the full Git commit SHA.
        AWS_REGION       = 'us-east-2'
        ECR_REPOSITORY   = 'sre-platform'
        DEPLOY_ROLE_NAME = 'sre-jenkins-deploy-role'
        // Docker client config (holds the registry login) lives in the credential tmpfs, not
        // in JENKINS_HOME. It is deleted in post { always }.
        DOCKER_CONFIG_DIR = '/run/aws-creds/docker-config'
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

        stage('Verify AWS Deployment Role') {
            steps {
                script {
                    env.GIT_SHA = sh(returnStdout: true, script: 'git rev-parse HEAD').trim()
                    if (!(env.GIT_SHA ==~ /^[0-9a-f]{40}$/)) {
                        error('Could not determine the full 40-character Git commit SHA.')
                    }
                    env.ECR_REGISTRY = sh(returnStdout: true, script: '''#!/bin/bash
                        set +x
                        set -euo pipefail
                        # Fails here, before any publishing, if no valid credentials are loaded.
                        arn=$(aws sts get-caller-identity --region "$AWS_REGION" --query Arn --output text)
                        case "$arn" in
                            *":assumed-role/${DEPLOY_ROLE_NAME}/"*) ;;
                            *) echo "Unexpected AWS identity (expected assumed-role/${DEPLOY_ROLE_NAME})" >&2; exit 1 ;;
                        esac
                        echo "Identity: ${arn##*:}" >&2
                        account=$(aws sts get-caller-identity --region "$AWS_REGION" --query Account --output text)
                        echo "${account}.dkr.ecr.${AWS_REGION}.amazonaws.com"
                    ''').trim()
                }
                echo "Commit tag: ${env.GIT_SHA}"
            }
        }

        stage('Publish Image to ECR') {
            steps {
                sh '''#!/bin/bash
                    set +x
                    set -euo pipefail
                    export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"
                    mkdir -p "$DOCKER_CONFIG"
                    chmod 700 "$DOCKER_CONFIG"
                    IMAGE="$ECR_REGISTRY/$ECR_REPOSITORY:$GIT_SHA"
                    DIGEST_RE='^sha256:[0-9a-f]{64}$'
                    TOOL="python3 jenkins/ecr_publish_tool.py"
                    RECORD="${JENKINS_HOME:?}/ecr-publish-records/$ECR_REPOSITORY/$GIT_SHA.record"
                    rm -f ecr-publish-state.env ecr-check.err ecr-existing-digest.txt

                    # The password goes through a pipe to --password-stdin, never an argument.
                    aws ecr get-login-password --region "$AWS_REGION" \
                        | docker login --username AWS --password-stdin "$ECR_REGISTRY"

                    # ECR tags are IMMUTABLE. Decide between "push" and "reuse" from what ECR says.
                    if aws ecr describe-images --region "$AWS_REGION" --repository-name "$ECR_REPOSITORY" \
                            --image-ids imageTag="$GIT_SHA" \
                            --query 'imageDetails[0].imageDigest' --output text \
                            >ecr-existing-digest.txt 2>ecr-check.err; then
                        existing=$(cat ecr-existing-digest.txt)
                        if ! [[ "$existing" =~ $DIGEST_RE ]]; then
                            echo "ECR returned an unexpected digest value for tag $GIT_SHA" >&2
                            exit 1
                        fi

                        echo "Tag $GIT_SHA already exists in $ECR_REPOSITORY."
                        echo "It is immutable: nothing will be pushed, overwritten or deleted. Checking whether it can be reused."

                        # Expected digest must come from a previous publish record or the operator,
                        # never from ECR itself. decide-reuse refuses mismatches and never implies a write.
                        recorded=""
                        rc=0
                        recorded=$($TOOL record-read "$RECORD") || rc=$?
                        case "$rc" in
                            0) ;;
                            4) recorded="" ;;
                            *) echo "The publish record for this commit is unreadable or malformed." >&2; exit 1 ;;
                        esac
                        $TOOL decide-reuse \
                            --existing "$existing" \
                            --recorded "$recorded" \
                            --supplied "${EXPECTED_IMAGE_DIGEST:-}" \
                            > ecr-publish-state.env
                        echo "Reusing the existing image. Digest matches the expected value ($(sed -n 's/^TRUST=//p' ecr-publish-state.env)):"
                        echo "  $existing"

                    elif grep -q ImageNotFoundException ecr-check.err; then
                        echo "Tag $GIT_SHA is not in $ECR_REPOSITORY yet. Building and pushing."
                        rm -f ecr-build-metadata.json
                        # Plain image manifest (no attestations) for linux/amd64, built and pushed with Buildx.
                        # The revision label lets a later build check this image against its commit.
                        docker buildx build \
                            --platform linux/amd64 \
                            --provenance=false \
                            --sbom=false \
                            --label "org.opencontainers.image.revision=$GIT_SHA" \
                            --tag "$IMAGE" \
                            --metadata-file ecr-build-metadata.json \
                            --push \
                            .

                        digest=$(python3 -c 'import json;print(json.load(open("ecr-build-metadata.json"))["containerimage.digest"])')
                        if ! [[ "$digest" =~ $DIGEST_RE ]]; then
                            echo "Unexpected digest format from Buildx" >&2
                            exit 1
                        fi
                        echo "Pushed $ECR_REPOSITORY:$GIT_SHA"
                        echo "Pushed digest: $digest"

                        # Record it immediately, so a failure in the next stage cannot orphan the image.
                        # If a record exists but the tag was absent, the old image expired (lifecycle policy).
                        $TOOL record-write "$RECORD" --repository "$ECR_REPOSITORY" --sha "$GIT_SHA" \
                            --digest "$digest" --status pushed --source pipeline-push \
                            --job "$JOB_NAME" --build "$BUILD_NUMBER" --replace-stale
                        {
                            echo "MODE=pushed"
                            echo "DIGEST=$digest"
                            echo "TRUST=pipeline-push"
                        } > ecr-publish-state.env

                    else
                        echo "Could not check for an existing tag:" >&2
                        cat ecr-check.err >&2
                        exit 1
                    fi
                    rm -f ecr-check.err ecr-existing-digest.txt
                '''
            }
        }

        stage('Verify Image in ECR') {
            steps {
                sh '''#!/bin/bash
                    set +x
                    set -euo pipefail
                    export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"
                    IMAGE="$ECR_REGISTRY/$ECR_REPOSITORY:$GIT_SHA"
                    TOOL="python3 jenkins/ecr_publish_tool.py"
                    RECORD="${JENKINS_HOME:?}/ecr-publish-records/$ECR_REPOSITORY/$GIT_SHA.record"
                    mode=$(sed -n 's/^MODE=//p' ecr-publish-state.env)
                    expected=$(sed -n 's/^DIGEST=//p' ecr-publish-state.env)
                    trust=$(sed -n 's/^TRUST=//p' ecr-publish-state.env)
                    echo "Mode: $mode (digest source: $trust)"

                    # ecr:DescribeImages is the source of truth for what ECR holds.
                    read -r digest pushed_at size < <(aws ecr describe-images \
                        --region "$AWS_REGION" --repository-name "$ECR_REPOSITORY" \
                        --image-ids imageTag="$GIT_SHA" \
                        --query 'imageDetails[0].[imageDigest,imagePushedAt,imageSizeInBytes]' --output text)
                    echo "ECR digest:    $digest"
                    echo "ECR pushed at: $pushed_at"
                    echo "ECR size:      $size bytes"
                    if [ "$digest" != "$expected" ]; then
                        echo "Digest mismatch: expected $expected but ECR reports $digest" >&2
                        exit 1
                    fi

                    # Read the image config as JSON. The default imagetools text listing does not always
                    # print a "Platform:" line for a single-platform OCI image (Build #1 failed that way
                    # even though the config was linux/amd64), so we never grep the text output.
                    if ! docker buildx imagetools inspect --format '{{json .Image}}' "$IMAGE" > ecr-image-config.json 2> ecr-inspect.err; then
                        echo "Could not read the image config from ECR:" >&2
                        cat ecr-inspect.err >&2
                        exit 1
                    fi
                    rm -f ecr-inspect.err

                    # A freshly pushed image must carry the revision label we just set. A reused image must not
                    # carry a different one. One built before labels existed has none, and is trusted only
                    # because its digest matched a record or an operator-supplied value.
                    label_flag=""
                    if [ "$mode" = pushed ]; then label_flag="--require-label"; fi
                    $TOOL check-config --expect-sha "$GIT_SHA" $label_flag < ecr-image-config.json | tee ecr-image-check.txt

                    case "$trust" in
                        operator-attested) record_source=operator-attested ;;
                        *)                 record_source=pipeline-push ;;
                    esac
                    $TOOL record-write "$RECORD" --repository "$ECR_REPOSITORY" --sha "$GIT_SHA" --digest "$digest" --status verified --source "$record_source" --job "$JOB_NAME" --build "$BUILD_NUMBER"

                    {
                        echo "repository=$ECR_REPOSITORY"
                        echo "tag=$GIT_SHA"
                        echo "digest=$digest"
                        echo "mode=$mode"
                        echo "digest_source=$trust"
                        cat ecr-image-check.txt
                        echo "pushed_at=$pushed_at"
                    } > ecr-image-record.txt
                    echo "Recorded in ecr-image-record.txt"
                '''
                archiveArtifacts artifacts: 'ecr-image-record.txt', fingerprint: false
            }
        }

        stage('Deploy to EC2') {
            options {
                timeout(time: 10, unit: 'MINUTES')
            }
            steps {
                // The digest is the one Verify Image in ECR just recorded. The send
                // request is built by jenkins/ssm_deploy_tool.py, which allows only
                // SREPlatformDeploy version 1 and instance i-0de56d01b124c0e69.
                sh '''#!/bin/bash
                    set +x
                    set -euo pipefail
                    tool="python3 jenkins/ssm_deploy_tool.py"
                    digest=$(sed -n 's/^digest=//p' ecr-image-record.txt)
                    rm -f ssm-identity.err ssm-invoke.err ssm-send.json ssm-invocation.json

                    set +e
                    arn=$(aws sts get-caller-identity --region "$AWS_REGION" --query Arn --output text 2>ssm-identity.err)
                    id_rc=$?
                    set -e
                    if [ "$id_rc" -ne 0 ]; then
                        $tool explain-auth-failure < ssm-identity.err >&2
                        exit 1
                    fi
                    $tool check-identity "$arn"
                    $tool validate-digest "$digest"
                    $tool write-send ssm-send.json "$digest"
                    instance_id=$(python3 -c 'import json; print(json.load(open("ssm-send.json"))["InstanceIds"][0])')

                    command_id=$(aws ssm send-command --cli-input-json file://ssm-send.json --query Command.CommandId --output text)
                    $tool validate-command-id "$command_id"
                    echo "SSM command: $command_id"

                    start=$(date +%s)
                    while true; do
                        elapsed=$(( $(date +%s) - start ))
                        set +e
                        aws ssm get-command-invocation \
                            --command-id "$command_id" \
                            --instance-id "$instance_id" \
                            --output json > ssm-invocation.json 2>ssm-invoke.err
                        get_rc=$?
                        set -e
                        if [ "$get_rc" -ne 0 ]; then
                            if $tool retryable-error < ssm-invoke.err && [ "$elapsed" -lt 30 ]; then
                                sleep 2
                                continue
                            fi
                            $tool explain-auth-failure < ssm-invoke.err >&2
                            exit 1
                        fi
                        set +e
                        $tool classify ssm-invocation.json --digest "$digest" --elapsed "$elapsed"
                        class_rc=$?
                        set -e
                        if [ "$class_rc" -eq 0 ]; then
                            break
                        fi
                        if [ "$class_rc" -eq 10 ]; then
                            sleep 5
                            continue
                        fi
                        exit 1
                    done
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
            // Remove the registry login and the temporary AWS credentials, pass or fail.
            // Removes only a local reference on the dind daemon. Nothing in ECR is ever deleted.
            sh '''
                if [ -n "${ECR_REGISTRY:-}" ] && [ -n "${GIT_SHA:-}" ]; then
                    ref="$ECR_REGISTRY/$ECR_REPOSITORY:$GIT_SHA"
                    if docker image inspect "$ref" >/dev/null 2>&1; then
                        docker rmi -f "$ref" || true
                    fi
                fi
                rm -rf "$DOCKER_CONFIG_DIR"
                rm -f ecr-check.err ecr-inspect.err ecr-existing-digest.txt
                rm -f ssm-identity.err ssm-invoke.err ssm-send.json ssm-invocation.json
                jenkins-aws-clear || true
            '''
        }
    }
}
