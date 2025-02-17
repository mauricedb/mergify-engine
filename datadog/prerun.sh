#!/usr/bin/env bash

case $DYNOTYPE in
    run)
        DISABLE_DATADOG_AGENT="true"
        ;;
    web)
        cat > "$DATADOG_CONF" <<EOF
confd_path: $DD_CONF_DIR/conf.d
logs_enabled: true
additional_checksd: $DD_CONF_DIR/checks.d
tags:
  - dyno:$DYNO
  - dynotype:$DYNOTYPE
  - buildpackversion:$BUILDPACKVERSION
  - appname:$HEROKU_APP_NAME
  - service:web
EOF
        cat >> "$DD_CONF_DIR/conf.d/process.d/conf.yaml" <<EOF
  - name: gunicorn-worker
    search_string: ['^gunicorn: worker']
    exact_match: false
    tags:
      - service:web
  - name: gunicorn-master
    search_string: ['^gunicorn: master']
    exact_match: false
    tags:
      - service:web
EOF
        ;;
    engine)
        cat > "$DATADOG_CONF" <<EOF
confd_path: $DD_CONF_DIR/conf.d
logs_enabled: true
additional_checksd: $DD_CONF_DIR/checks.d
tags:
  - dyno:$DYNO
  - dynotype:$DYNOTYPE
  - buildpackversion:$BUILDPACKVERSION
  - appname:$HEROKU_APP_NAME
  - service:celery
EOF
        cat >> "$DD_CONF_DIR/conf.d/process.d/conf.yaml" <<EOF
  - name: celery-main
    # [celeryd: celery@aeade076-e94d-452f-8af0-ad8d5850fa4c:MainProcess] -active- (worker --beat --app mergifyio.synchronizator --concurrency 4 --queues schedule,github.accounts,github.events,celery)
    search_string: ['\[celeryd: .+:MainProcess\]']
    exact_match: false
    tags:
      - service:celery
  - name: celery-worker
    # [celeryd: celery@aeade076-e94d-452f-8af0-ad8d5850fa4c:ForkPoolWorker-2]
    search_string: ['\[celeryd: .+:ForkPoolWorker']
    exact_match: false
    tags:
      - service:celery
  - name: celery-beat
    # celery-beat
    search_string: ['[celery beat]']
    tags:
      - service:celery
EOF
        ;;
esac

# If we are in ps:exec do nothing plz
# see https://github.com/DataDog/heroku-buildpack-datadog/issues/155
if [ -n "$DYNO" ]; then
    # Workaround for https://github.com/DataDog/heroku-buildpack-datadog/issues/155
    # When datadog.sh is called it will copy the example and overwrite our conf
    cp "$DATADOG_CONF" "$DATADOG_CONF.example"

    if [ -n $"MERGIFYENGINE_LOG_JSON_FILE" ]; then
        sed -i "s,<MERGIFYENGINE_LOG_JSON_FILE>,${MERGIFYENGINE_LOG_JSON_FILE},g" $DD_CONF_DIR/conf.d/python.d/conf.yaml
    fi

    REDIS_REGEX='^redis://([^:]+):([^@]+)@([^:]+):([^/]+)$'

    if [ -n "$MERGIFYENGINE_STORAGE_URL" ]; then
        if [[ $MERGIFYENGINE_STORAGE_URL =~ $REDIS_REGEX ]]; then
            sed -i "s/<CACHE HOST>/${BASH_REMATCH[3]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
            sed -i "s/<CACHE PASSWORD>/${BASH_REMATCH[2]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
            sed -i "s/<CACHE PORT>/${BASH_REMATCH[4]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        fi
    fi


    if [ -n "$MERGIFYENGINE_CELERY_BROKER_URL" ]; then
        if [[ $MERGIFYENGINE_CELERY_BROKER_URL =~ $REDIS_REGEX ]]; then
            sed -i "s/<CELERY HOST>/${BASH_REMATCH[3]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
            sed -i "s/<CELERY PASSWORD>/${BASH_REMATCH[2]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
            sed -i "s/<CELERY PORT>/${BASH_REMATCH[4]}/" "$DD_CONF_DIR/conf.d/redisdb.d/conf.yaml"
        fi
    fi
fi
