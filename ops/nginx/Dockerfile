FROM nginx:1.27-alpine

RUN apk add --no-cache openssl gettext

COPY entrypoint.sh /docker-entrypoint-custom.sh
RUN chmod +x /docker-entrypoint-custom.sh

COPY nginx.conf /etc/nginx/nginx.conf

ENTRYPOINT ["/docker-entrypoint-custom.sh"]
