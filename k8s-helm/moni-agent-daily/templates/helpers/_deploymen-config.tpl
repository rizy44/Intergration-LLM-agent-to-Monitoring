{{- define "deploy-bot.deployment" -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "deploy-bot.fullname" . }}-{{ .name }}
  labels:
    {{- include "deploy-bot.labels" . | nindent 4 }}
spec:
  replicas: {{ .config.replicaCount | default 1 }}
  revisionHistoryLimit: {{ .config.revisionHistoryLimit | default 3 }}
  selector:
    matchLabels:
      {{- include "deploy-bot.selectorLabels" . | nindent 6 }}
  template:
    metadata:
      labels:
        {{- include "deploy-bot.selectorLabels" . | nindent 8 }}
    spec:
      containers:
        - name: {{ .name }}
          image: "{{ .config.image.repository }}:{{ .config.image.tag | default .Chart.AppVersion }}"
          imagePullPolicy: {{ .config.image.pullPolicy | default "IfNotPresent" }}
          {{- with .config.command }}
          command:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.args }}
          args:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.env }}
          env:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.service }}
          ports:
            - containerPort: {{ .targetPort | default .port }}
              protocol: {{ .protocol | default "TCP" }}
              name: {{ .name | default $.name }}
          {{- end }}
          {{- with .config.livenessProbe }}
          livenessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.readinessProbe }}
          readinessProbe:
            {{- toYaml . | nindent 12 }}
          {{- end }}
          {{- with .config.resources }}
          resources:
            {{- toYaml . | nindent 12 }}
          {{- end }}
      {{- with .config.imagePullSecrets }}
      imagePullSecrets:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .config.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .config.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- else }}
      {{- with $.Values.affinity }}
      affinity:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- end }}
      {{- with .config.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
{{- end }}
