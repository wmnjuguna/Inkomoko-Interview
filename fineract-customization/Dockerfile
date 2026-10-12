# syntax=docker/dockerfile:1.7
#
# Builds Apache Fineract from source at a pinned release tag and packages it as our own image.
# The tag is cloned here (not copied from the submodule) so Gradle has real git metadata,
# and the build fails if the tag no longer points at the pinned commit.

ARG JDK_IMAGE=eclipse-temurin:21-jdk-alpine
ARG JRE_IMAGE=eclipse-temurin:21-jre-alpine

FROM ${JDK_IMAGE} AS build
ARG FINERACT_TAG=1.15.0
ARG FINERACT_COMMIT=d5636847ac556c30b437254c353f05526d172b97
ARG GRADLE_XMX=6g

RUN apk add --no-cache git bash

WORKDIR /src
RUN git clone --depth 1 --branch "${FINERACT_TAG}" https://github.com/apache/fineract.git . \
 && test "$(git rev-parse HEAD)" = "${FINERACT_COMMIT}" \
 || (echo "Tag ${FINERACT_TAG} does not point at ${FINERACT_COMMIT}" >&2 && exit 1)

# Upstream gradle.properties asks for a 12 GB heap; keep the same flags with a smaller heap.
ENV GRADLE_JVM_ARGS="--add-exports jdk.compiler/com.sun.tools.javac.api=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.file=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.parser=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.tree=ALL-UNNAMED --add-exports jdk.compiler/com.sun.tools.javac.util=ALL-UNNAMED --add-exports=java.naming/com.sun.jndi.ldap=ALL-UNNAMED --add-opens=java.base/java.lang=ALL-UNNAMED --add-opens=java.base/java.lang.invoke=ALL-UNNAMED --add-opens=java.base/java.io=ALL-UNNAMED --add-opens=java.base/java.security=ALL-UNNAMED --add-opens=java.base/java.util=ALL-UNNAMED --add-opens=java.management/javax.management=ALL-UNNAMED --add-opens=java.naming/javax.naming=ALL-UNNAMED"

# Our modules join Fineract's build through its custom/<company>/<category>/<module> convention.
COPY custom/inkomoko /src/custom/inkomoko

RUN --mount=type=cache,target=/root/.gradle \
    ./gradlew --no-daemon --max-workers=3 \
      "-Dorg.gradle.jvmargs=-Xmx${GRADLE_XMX} ${GRADLE_JVM_ARGS}" \
      :custom:inkomoko:glsync:service:test :custom:inkomoko:glsync:service:integrationTestBundle \
      :fineract-provider:bootJar \
      :custom:inkomoko:glsync:service:jar :custom:inkomoko:glsync:starter:jar \
 && mkdir -p /out/libs \
 && cp "$(ls -S fineract-provider/build/libs/fineract-provider-*.jar | grep -v plain | head -1)" /out/fineract-provider.jar \
 && find custom/inkomoko -path '*/build/libs/*.jar' ! -name '*-sources.jar' ! -name '*-javadoc.jar' -exec cp {} /out/libs/ \; \
 && cp -r custom/inkomoko/glsync/service/build/integration-test-bundle /out/integration-test \
 && ls -l /out/libs

# Integration tests as a small runnable image: compiled tests + JUnit, AssertJ, Gson. No Gradle, no rebuild.
#   docker buildx build --load --target integration-test -t fineract-inkomoko-it:1.15.0 fineract-customization
#   docker run --rm --network inkomoko-fineract_default -e FINERACT_URL=http://fineract:8080 fineract-inkomoko-it:1.15.0
FROM ${JRE_IMAGE} AS integration-test
COPY --from=build /out/integration-test /it
ENTRYPOINT ["java", "-cp", "/it/classes:/it/lib/*", "org.junit.platform.console.ConsoleLauncher", "execute", \
            "--select-package", "com.inkomoko.fineract.glsync.integration", "--details", "tree", "--disable-banner", "--disable-ansi-colors"]

FROM ${JRE_IMAGE}
ARG FINERACT_TAG=1.15.0
ARG FINERACT_COMMIT=d5636847ac556c30b437254c353f05526d172b97
LABEL org.opencontainers.image.title="fineract-inkomoko" \
      org.opencontainers.image.version="${FINERACT_TAG}" \
      org.opencontainers.image.revision="${FINERACT_COMMIT}"

RUN apk add --no-cache curl

WORKDIR /app
COPY --from=build /out/fineract-provider.jar /app/fineract-provider.jar
# Our module jars; Spring Boot's PropertiesLauncher adds them to the classpath via loader.path.
COPY --from=build /out/libs/ /app/libs/

USER nobody
EXPOSE 8080
ENTRYPOINT ["java", "-Dloader.path=/app/libs", "-Duser.home=/tmp", "-Dfile.encoding=UTF-8", "-Duser.timezone=UTC", "-Djava.security.egd=file:/dev/./urandom", "-jar", "/app/fineract-provider.jar"]
