"""Hyperstack Cloud."""
import os
import typing
from typing import Dict, Iterator, List, Optional, Tuple, Union

import requests

from sky import clouds
from sky.clouds import service_catalog
from sky.provision.hyperstack import hyperstack_utils
from sky.utils import registry
from sky.utils import resources_utils

if typing.TYPE_CHECKING:
    # Renaming to avoid shadowing variables.
    from sky import resources as resources_lib

# Minimum set of files under ~/.hyperstack that grant Hyperstack Cloud access.
_CREDENTIAL_FILES = [
    'api_key',
]


@registry.CLOUD_REGISTRY.register
class Hyperstack(clouds.Cloud):
    """Hyperstack Labs GPU Cloud."""

    _REPR = 'Hyperstack'

    # Hyperstack has a 50 char limit for cluster name.
    # Reference: https://infrahub-doc.nexgencloud.com/docs/api-reference/core-resources/virtual-machines/vm-core/create-vms # pylint: disable=line-too-long
    # However, we need to account for the suffixes '-head' and '-worker'
    _MAX_CLUSTER_NAME_LEN_LIMIT = 43
    _CLOUD_UNSUPPORTED_FEATURES = {
        clouds.CloudImplementationFeatures.CLONE_DISK_FROM_CLUSTER: f'Migrating disk is currently not supported on {_REPR}.',
        clouds.CloudImplementationFeatures.SPOT_INSTANCE: f'Spot instances are not supported in {_REPR}.',
        clouds.CloudImplementationFeatures.IMAGE_ID: f'Specifying image ID is not supported in {_REPR}.',
        clouds.CloudImplementationFeatures.CUSTOM_DISK_TIER: f'Custom disk tiers are not supported in {_REPR}.',
        clouds.CloudImplementationFeatures.HOST_CONTROLLERS: f'Host controllers are not supported in {_REPR}.',
        # TODO: implement storage mounting
        clouds.CloudImplementationFeatures.STORAGE_MOUNTING: f'Storage mounting is currently not supported in {_REPR}.',
    }

    PROVISIONER_VERSION = clouds.ProvisionerVersion.SKYPILOT
    STATUS_VERSION = clouds.StatusVersion.SKYPILOT

    @classmethod
    def _unsupported_features_for_resources(
        cls, resources: 'resources_lib.Resources'
    ) -> Dict[clouds.CloudImplementationFeatures, str]:
        del resources  # unused
        return cls._CLOUD_UNSUPPORTED_FEATURES

    @classmethod
    def max_cluster_name_length(cls) -> Optional[int]:
        return cls._MAX_CLUSTER_NAME_LEN_LIMIT

    @classmethod
    def regions_with_offering(cls, instance_type: str,
                              accelerators: Optional[Dict[str, int]],
                              use_spot: bool, region: Optional[str],
                              zone: Optional[str]) -> List[clouds.Region]:
        assert zone is None, 'Hyperstack does not support zones.'
        del accelerators, zone  # unused
        if use_spot:
            return []
        regions = service_catalog.get_region_zones_for_instance_type(
            instance_type, use_spot, 'Hyperstack')

        if region is not None:
            regions = [r for r in regions if r.name == region]
        return regions

    @classmethod
    def get_vcpus_mem_from_instance_type(
        cls,
        instance_type: str,
    ) -> Tuple[Optional[float], Optional[float]]:
        return service_catalog.get_vcpus_mem_from_instance_type(
            instance_type, clouds='hyperstack')

    @classmethod
    def zones_provision_loop(
        cls,
        *,
        region: str,
        num_nodes: int,
        instance_type: str,
        accelerators: Optional[Dict[str, int]] = None,
        use_spot: bool = False,
    ) -> Iterator[None]:
        del num_nodes  # unused
        regions = cls.regions_with_offering(instance_type,
                                            accelerators,
                                            use_spot,
                                            region=region,
                                            zone=None)
        for r in regions:
            assert r.zones is None, r
            yield r.zones

    def instance_type_to_hourly_cost(self,
                                     instance_type: str,
                                     use_spot: bool,
                                     region: Optional[str] = None,
                                     zone: Optional[str] = None) -> float:
        return service_catalog.get_hourly_cost(instance_type,
                                               use_spot=use_spot,
                                               region=region,
                                               zone=zone,
                                               clouds='hyperstack')

    def accelerators_to_hourly_cost(self,
                                    accelerators: Dict[str, int],
                                    use_spot: bool,
                                    region: Optional[str] = None,
                                    zone: Optional[str] = None) -> float:
        del accelerators, use_spot, region, zone  # unused
        # Hyperstack includes accelerators as part of the instance type.
        return 0.0

    def get_egress_cost(self, num_gigabytes: float) -> float:
        return 0.0

    def __repr__(self):
        return 'Hyperstack'

    @classmethod
    def get_default_instance_type(
        cls,
        cpus: Optional[str] = None,
        memory: Optional[str] = None,
        disk_tier: Optional['resources_utils.DiskTier'] = None
    ) -> Optional[str]:
        return service_catalog.get_default_instance_type(cpus=cpus,
                                                         memory=memory,
                                                         disk_tier=disk_tier,
                                                         clouds='hyperstack')

    @classmethod
    def get_accelerators_from_instance_type(
        cls,
        instance_type: str,
    ) -> Optional[Dict[str, Union[int, float]]]:
        return service_catalog.get_accelerators_from_instance_type(
            instance_type, clouds='hyperstack')

    @classmethod
    def get_zone_shell_cmd(cls) -> Optional[str]:
        return None

    def make_deploy_resources_variables(
            self,
            resources: 'resources_lib.Resources',
            cluster_name: 'resources_utils.ClusterName',
            region: 'clouds.Region',
            zones: Optional[List['clouds.Zone']],
            num_nodes: int,
            dryrun: bool = False) -> Dict[str, Optional[str]]:
        del cluster_name, dryrun  # Unused.
        assert zones is None, 'Hyperstack does not support zones.'

        r = resources
        acc_dict = self.get_accelerators_from_instance_type(r.instance_type)
        custom_resources = resources_utils.make_ray_custom_resources_str(
            acc_dict)

        resources_vars = {
            'instance_type': resources.instance_type,
            'custom_resources': custom_resources,
            'region': region.name,
        }

        if acc_dict is not None:
            # Hyperstack cloud's docker runtime information does not contain
            # 'nvidia-container-runtime', causing no GPU option is added to
            # the docker run command. We patch this by adding it here.
            resources_vars['docker_run_options'] = ['--gpus all']

        return resources_vars

    def _get_feasible_launchable_resources(
        self, resources: 'resources_lib.Resources'
    ) -> 'resources_utils.FeasibleResources':
        if resources.instance_type is not None:
            assert resources.is_launchable(), resources
            # Accelerators are part of the instance type in Hyperstack Cloud
            resources = resources.copy(accelerators=None)
            # TODO: Add hints to all return values in this method to help
            #  users understand why the resources are not launchable.
            return resources_utils.FeasibleResources([resources], [], None)

        def _make(instance_list):
            resource_list = []
            for instance_type in instance_list:
                r = resources.copy(
                    cloud=Hyperstack(),
                    instance_type=instance_type,
                    # Setting this to None as Hyperstack doesn't separately bill /
                    # attach the accelerators.  Billed as part of the VM type.
                    accelerators=None,
                    cpus=None,
                    memory=None,
                )
                resource_list.append(r)
            return resource_list

        # Currently, handle a filter on accelerators only.
        accelerators = resources.accelerators
        if accelerators is None:
            # Return a default instance type with the given number of vCPUs.
            default_instance_type = Hyperstack.get_default_instance_type(
                cpus=resources.cpus,
                memory=resources.memory,
                disk_tier=resources.disk_tier)
            if default_instance_type is None:
                return resources_utils.FeasibleResources([], [], None)
            else:
                return resources_utils.FeasibleResources(
                    _make([default_instance_type]), [], None)

        assert len(accelerators) == 1, resources
        acc, acc_count = list(accelerators.items())[0]
        (instance_list, fuzzy_candidate_list
        ) = service_catalog.get_instance_type_for_accelerator(
            acc,
            acc_count,
            use_spot=resources.use_spot,
            cpus=resources.cpus,
            memory=resources.memory,
            region=resources.region,
            zone=resources.zone,
            clouds='hyperstack')
        if instance_list is None:
            return resources_utils.FeasibleResources([], fuzzy_candidate_list,
                                                     None)
        return resources_utils.FeasibleResources(_make(instance_list),
                                                 fuzzy_candidate_list, None)

    @classmethod
    def _check_compute_credentials(cls) -> Tuple[bool, Optional[str]]:
        """Checks if the user has access credentials to
        Hyperstack's compute service."""
        return cls._check_credentials()

    @classmethod
    def _check_storage_credentials(cls) -> Tuple[bool, Optional[str]]:
        """Checks if the user has access credentials to
        Hyperstack's storage service."""
        return cls._check_credentials()

    @classmethod
    def _check_credentials(cls) -> Tuple[bool, Optional[str]]:
        try:
            assert os.path.exists(
                os.path.expanduser(hyperstack_utils.HYPERSTACK_API_KEY_PATH))
            hyperstack_utils.HyperstackClient().list_instances()
        except AssertionError:
            return False, ('Failed to access Hyperstack Cloud'
                           ' with credentials. '
                           'To configure credentials, go to:\n    '
                           '  https://console.hyperstack.cloud/api-keys \n    '
                           'to obtain an API key, '
                           'then add save the contents '
                           'to ~/.hyperstack/api_key \n')
        except requests.exceptions.ConnectionError:
            return False, ('Failed to verify Hyperstack Cloud credentials. '
                           'Check your network connection '
                           'and try again.')
        return True, None

    def get_credential_file_mounts(self) -> Dict[str, str]:
        return {
            f'~/.hyperstack/{filename}': f'~/.hyperstack/{filename}'
            for filename in _CREDENTIAL_FILES
        }

    @classmethod
    def get_user_identities(cls) -> Optional[List[List[str]]]:
        # TODO(ewzeng): Implement get_user_identities for Hyperstack
        return None

    def instance_type_exists(self, instance_type: str) -> bool:
        return service_catalog.instance_type_exists(instance_type, 'hyperstack')

    def validate_region_zone(self, region: Optional[str], zone: Optional[str]):
        return service_catalog.validate_region_zone(region,
                                                    zone,
                                                    clouds='hyperstack')

    @classmethod
    def regions(cls) -> List['clouds.Region']:
        return service_catalog.regions(clouds='hyperstack')
