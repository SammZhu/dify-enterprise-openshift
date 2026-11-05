# jq 1.6+: input is a rendered Kubernetes resource (or a JSON value with images).
# --slurpfile image_map /private/image-map.json
# No cluster access and no installation side effects.
def regex_escape: gsub("(?<c>[\\\\.^$|?*+(){}\\[\\]])"; "\\" + .c);
def image_text:
  reduce ($image_map[0] | keys | sort | reverse[]) as $source
    (. ; gsub("(?<![A-Za-z0-9_./:@-])" + ($source | regex_escape) + "(?![A-Za-z0-9_.:/@-])"; $image_map[0][$source]));
def images: walk(if type == "string" then image_text else . end);
def pull: (. // []) + [{name:"offline-registry-pull"}] | unique_by(.name);
images
| if .kind? == "Secret" then
    .data |= ((. // {}) | with_entries(
      .value as $raw
      | (try ($raw | @base64d) catch null) as $decoded
      | if $decoded != null and ($decoded | @base64) == ($raw | gsub("\\s";""))
        then .value = ($decoded | image_text | @base64) else . end))
  else . end
| .kind as $kind
| if $kind == "ServiceAccount" then .imagePullSecrets |= pull
  elif $kind == "Pod" then .spec.imagePullSecrets |= pull
  elif (["Deployment","StatefulSet","DaemonSet","ReplicaSet","Job"] | index($kind)) != null
    then .spec.template.spec.imagePullSecrets |= pull
  elif $kind == "CronJob" then .spec.jobTemplate.spec.template.spec.imagePullSecrets |= pull
  else . end
| if $kind == "ConfigMap" and .metadata.name == "dify-dify-enterprise-plugin-daemon-config"
  then .data |= with_entries(if (.key | startswith("OTLP_")) and (.value | type) == "string"
    then .value |= sub("-collector-svc:4317$"; "-collector-svc:4318") else . end)
  else . end
