/**
 * GL account synchronisation from the ERP into Fineract.
 *
 * <p>Enforces, inside Fineract, that an ERP {@code external_reference} maps to exactly one GL account: an identical
 * payload returns the existing account, a different payload is rejected with 409, and the endpoint is guarded by the
 * {@code SYNC_GLACCOUNT} permission.
 */
package com.inkomoko.fineract.glsync;
