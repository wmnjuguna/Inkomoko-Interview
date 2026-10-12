package com.inkomoko.fineract.glsync.starter;

import java.util.Set;
import lombok.extern.slf4j.Slf4j;
import org.apache.fineract.infrastructure.core.config.jpa.EntityManagerFactoryCustomizer;
import org.springframework.boot.autoconfigure.AutoConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.ComponentScan;
import org.springframework.data.jpa.repository.config.EnableJpaRepositories;

/**
 * Wires the GL sync module into Fineract. Fineract's own JPA setup only scans {@code org.apache.fineract}, so the module
 * registers its entity package through Fineract's {@link EntityManagerFactoryCustomizer} hook and enables its own
 * repositories.
 */
@Slf4j
@AutoConfiguration
@ComponentScan(GlSyncAutoConfiguration.BASE_PACKAGE)
@EnableJpaRepositories(basePackages = GlSyncAutoConfiguration.DOMAIN_PACKAGE)
public class GlSyncAutoConfiguration {

    static final String BASE_PACKAGE = "com.inkomoko.fineract.glsync";
    static final String DOMAIN_PACKAGE = BASE_PACKAGE + ".domain";

    public GlSyncAutoConfiguration() {
        log.info("Inkomoko GL sync module loaded");
    }

    @Bean
    EntityManagerFactoryCustomizer glSyncEntityManagerFactoryCustomizer() {
        return new EntityManagerFactoryCustomizer() {

            @Override
            public Set<String> additionalPackagesToScan() {
                return Set.of(DOMAIN_PACKAGE);
            }
        };
    }
}
