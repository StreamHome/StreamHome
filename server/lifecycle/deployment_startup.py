"""Resolve the managed deployment selected by canonical startup authority."""
from __future__ import annotations

import os

from .configuration_store import ConfigurationStore, ConfigurationStoreError
from .deployment_config import DeploymentConfig
from .layout import RuntimeLayout


def deployment_for_start(layout: RuntimeLayout) -> DeploymentConfig:
    if not layout.managed:
        raise ConfigurationStoreError("Managed deployment selection requires a managed descriptor.")
    from . import state
    from .bootstrap import require_startup_context, require_current_guarded_authority
    from .management_journal import ActivationMode, validate_active_journal
    from .management_store import inspect_management_journal

    descriptor = layout.descriptor.descriptor
    directory = os.open(layout.domains, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        with ConfigurationStore(directory, descriptor.installation_id) as store:
            manifest = store.inspect()
            reference = manifest.active
            authority = state.inspect_state(layout.application)
            operation = authority.active_operation
            if operation is not None and operation.operation_type is state.OperationType.MANAGEMENT:
                context = require_startup_context()
                if context.mode.value != "management_verify":
                    raise ConfigurationStoreError("An unresolved management operation cannot select ordinary deployment settings.")
                require_current_guarded_authority(context)
                journal = inspect_management_journal(layout.application, operation.operation_id)
                validate_active_journal(journal, authority)
                from .management_journal import ManagementAction, selected_execution
                candidate_reference = (manifest.desired if journal.action is ManagementAction.CONFIGURE_APPLY
                    and journal.recovery_target == 'candidate' else manifest.active)
                if journal.activation_mode is not ActivationMode.CUTOVER or candidate_reference.revision != selected_execution(journal).deployment_revision:
                    raise ConfigurationStoreError("The guarded candidate does not match the reviewed desired deployment.")
                reference = candidate_reference
            if operation is not None and operation.operation_type is state.OperationType.RESTORE:
                context = require_startup_context()
                if context.mode.value != 'restore_verify':
                    raise ConfigurationStoreError('An unresolved restore cannot select ordinary deployment settings.')
                require_current_guarded_authority(context)
                from .managed_restore import bound_journal
                _,bound = bound_journal(layout.application,operation.operation_id)
                if reference.revision != bound.execution.deployment_revision:
                    raise ConfigurationStoreError('The restore deployment differs from its original execution.')
            if reference.revision in manifest.quarantined:
                raise ConfigurationStoreError("The selected deployment is quarantined.")
            return store.read_revision(reference).settings
    finally:
        os.close(directory)


def execution_for_start(layout: RuntimeLayout):
    """Select the same bound runtime as the canonical startup decision."""
    from . import state
    from .bootstrap import require_startup_context, require_current_guarded_authority
    from .execution_state import read_active_execution
    from .management_journal import ActivationMode, selected_execution, validate_active_journal
    from .management_store import inspect_management_journal
    if not layout.managed:
        raise ConfigurationStoreError("Managed runtime selection requires a managed descriptor.")
    authority = state.inspect_state(layout.application)
    operation = authority.active_operation
    if operation is not None and operation.operation_type is state.OperationType.MANAGEMENT:
        context = require_startup_context()
        if context.mode.value != "management_verify":
            raise ConfigurationStoreError("An unresolved management operation cannot select an ordinary runtime.")
        require_current_guarded_authority(context)
        journal = inspect_management_journal(layout.application, operation.operation_id)
        validate_active_journal(journal, authority)
        if journal.activation_mode is not ActivationMode.CUTOVER:
            raise ConfigurationStoreError("Only a guarded cutover can select candidate runtime artifacts.")
        return selected_execution(journal)
    if operation is not None and operation.operation_type is state.OperationType.RESTORE:
        context = require_startup_context()
        if context.mode.value != 'restore_verify':
            raise ConfigurationStoreError('An unresolved restore cannot select an ordinary runtime.')
        require_current_guarded_authority(context)
        from .managed_restore import bound_journal
        _,bound = bound_journal(layout.application,operation.operation_id)
        if read_active_execution(layout.application).execution != bound.execution:
            raise ConfigurationStoreError('The restore execution differs from its original binding.')
        return bound.execution
    # Update and restore startup remain guarded by their existing controller
    # protocols; their selected execution must be durably published separately.
    return read_active_execution(layout.application).execution
