# shellcheck shell=bash
# Helpers for the s6 run script (sourced). Kept separate so they can be tested without bashio.

# Print the trimmed value of .mqtt.host in the options file ("" if missing, null or blank).
# The same rule as the app model: a blank host means "use the Home Assistant MQTT service".
deye_mqtt_option_host() {
    jq -r '(.mqtt.host // "") | tostring | gsub("^\\s+|\\s+$"; "")' "${1:-/data/options.json}"
}

# Export DEYE_MONITOR_SUPERVISOR_MQTT_{HOST,PORT,USERNAME,PASSWORD,SSL} from the service JSON
# object (the output of `bashio::services mqtt`). A field that is missing or JSON null stays
# unexported; a string (including the text "null") is passed as it is. Nothing is printed.
deye_export_supervisor_mqtt() {
    local service="${1}" pair key name value
    for pair in host:HOST port:PORT username:USERNAME password:PASSWORD ssl:SSL; do
        key="${pair%%:*}"
        name="DEYE_MONITOR_SUPERVISOR_MQTT_${pair##*:}"
        value="$(jq -r --arg k "${key}" 'if has($k) and .[$k] != null then .[$k] | tostring else empty end' <<<"${service}" 2>/dev/null)" || value=""
        if [[ -n "${value}" ]]; then
            export "${name}=${value}"
        else
            unset "${name}"
        fi
    done
}
