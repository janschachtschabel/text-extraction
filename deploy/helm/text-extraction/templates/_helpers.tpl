{{/* Chart name, release-scoped full name and labels. */}}
{{- define "text-extraction.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "text-extraction.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Labels shared by every object. */}}
{{- define "text-extraction.labels" -}}
helm.sh/chart: {{ include "text-extraction.chart" . }}
{{ include "text-extraction.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Pod selector of the API Deployment. Never changes after the first install. */}}
{{- define "text-extraction.selectorLabels" -}}
app.kubernetes.io/name: {{ include "text-extraction.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Names of the Services, the Secret and the Valkey objects. */}}
{{- define "text-extraction.internalServiceName" -}}
{{- printf "%s-internal" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.valkeyName" -}}
{{- printf "%s-valkey" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "text-extraction.valkeySelectorLabels" -}}
app.kubernetes.io/name: {{ include "text-extraction.name" . }}-valkey
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Secret that holds API_KEYS: the existing one, or the chart-managed one. */}}
{{- define "text-extraction.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-keys" (include "text-extraction.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/* True when the service gets API_KEYS from a Secret. */}}
{{- define "text-extraction.hasApiKeys" -}}
{{- if or .Values.secrets.existingSecret .Values.secrets.apiKeys -}}true{{- end -}}
{{- end -}}

{{/* Image reference; the tag defaults to the chart appVersion. */}}
{{- define "text-extraction.image" -}}
{{- printf "%s:%s" .Values.image.repository (default .Chart.AppVersion .Values.image.tag) -}}
{{- end -}}

{{/* In-cluster Valkey URL. */}}
{{- define "text-extraction.valkeyUrl" -}}
{{- printf "redis://%s:6379/0" (include "text-extraction.valkeyName" .) -}}
{{- end -}}
