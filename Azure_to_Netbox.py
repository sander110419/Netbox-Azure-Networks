#!/usr/bin/env python3

import os
import sys
import logging
import argparse
from azure.identity import DefaultAzureCredential, InteractiveBrowserCredential
from azure.mgmt.subscription import SubscriptionClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.compute import ComputeManagementClient
from pynetbox import api
from pynetbox.core.query import RequestError
import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def truncate_name(name, max_length=64):
    """
    Truncate a name by:
    1. Removing everything after and including the first decimal point
    2. Ensuring the result doesn't exceed max_length characters
    """
    # First, remove everything after and including the first decimal point
    if '.' in name:
        name = name.split('.')[0]
        logger.debug(f"Removed decimal portion, new name: {name}")
    
    # Then ensure it doesn't exceed max_length
    if len(name) > max_length:
        logger.warning(f"Name '{name}' exceeds {max_length} characters, truncating")
        name = name[:max_length]
    
    return name

def get_azure_credentials(use_interactive=False):
    """Get Azure credentials based on environment"""
    if use_interactive:
        logger.info("Using interactive browser authentication for Azure")
        return InteractiveBrowserCredential()
    else:
        logger.info("Using default Azure credential chain")
        return DefaultAzureCredential()

def get_azure_subscriptions(credential):
    """Get all Azure subscriptions accessible by the credentials"""
    logger.info("Getting Azure subscriptions")
    subscription_client = SubscriptionClient(credential)
    subscriptions = list(subscription_client.subscriptions.list())
    logger.info(f"Found {len(subscriptions)} subscriptions")
    return subscriptions

def get_vnets_and_subnets(subscription_id, credential):
    """Get all VNets and subnets in a subscription"""
    logger.info(f"Getting VNets and subnets for subscription {subscription_id}")
    network_client = NetworkManagementClient(credential, subscription_id)
    
    vnets = list(network_client.virtual_networks.list_all())
    logger.info(f"Found {len(vnets)} VNets in subscription {subscription_id}")
    
    vnet_data = []
    for vnet in vnets:
        vnet_info = {
            'name': vnet.name,
            'id': vnet.id,
            'resource_group': vnet.id.split('/')[4],
            'location': vnet.location,
            'address_space': [prefix for prefix in vnet.address_space.address_prefixes],
            'subnets': []
        }
        
        for subnet in vnet.subnets:
            subnet_info = {
                'name': subnet.name,
                'id': subnet.id,
                'address_prefix': subnet.address_prefix,
                'devices': []
            }
            vnet_info['subnets'].append(subnet_info)
        
        vnet_data.append(vnet_info)
    
    return vnet_data

def get_devices_in_subnet(subscription_id, credential, vnets_data):
    """Get all devices connected to each subnet"""
    logger.info(f"Getting devices for subscription {subscription_id}")
    network_client = NetworkManagementClient(credential, subscription_id)
    compute_client = ComputeManagementClient(credential, subscription_id)
    
    # Get all NICs in the subscription
    nics = list(network_client.network_interfaces.list_all())
    logger.info(f"Found {len(nics)} network interfaces in subscription {subscription_id}")
    
    # Get all VMs in the subscription
    vms = list(compute_client.virtual_machines.list_all())
    vm_dict = {vm.id: vm for vm in vms}
    
    # Map NICs to subnets and collect device info
    for nic in nics:
        if nic.ip_configurations:
            for ip_config in nic.ip_configurations:
                if ip_config.subnet:
                    subnet_id = ip_config.subnet.id
                    
                    # Find the corresponding subnet in our data structure
                    for vnet in vnets_data:
                        for subnet in vnet['subnets']:
                            if subnet['id'] == subnet_id:
                                # Find the VM this NIC is attached to
                                vm = None
                                if nic.virtual_machine:
                                    vm_id = nic.virtual_machine.id
                                    vm = vm_dict.get(vm_id)
                                
                                device_info = {
                                    'name': vm.name if vm else nic.name,
                                    'id': vm.id if vm else nic.id,
                                    'type': 'vm' if vm else 'network_interface',
                                    'ip_address': ip_config.private_ip_address,
                                    'mac_address': nic.mac_address,
                                    'resource_group': nic.id.split('/')[4],
                                    'location': nic.location,
                                    'os_type': vm.storage_profile.os_disk.os_type if vm else None
                                }
                                subnet['devices'].append(device_info)
    
    return vnets_data

def get_or_create_tag(nb, tag_name, tag_slug, tag_description):
    """Get or create a tag in Netbox"""
    # Try to find the tag first
    try:
        tag = nb.extras.tags.get(slug=tag_slug)
        if tag:
            logger.info(f"Found existing tag: {tag_slug}")
            return tag
    except Exception as e:
        logger.debug(f"Error getting tag {tag_slug}: {str(e)}")
    
    # Create the tag if it doesn't exist
    logger.info(f"Creating new tag: {tag_slug}")
    return nb.extras.tags.create(
        name=tag_name,
        slug=tag_slug,
        description=tag_description
    )

def get_or_create_prefix(nb, prefix_value, defaults):
    """Get or create a prefix in Netbox"""
    # First, try to get the prefix directly
    try:
        # Try to find the prefix by its exact value
        existing_prefixes = nb.ipam.prefixes.filter(prefix=prefix_value)
        
        if existing_prefixes:
            logger.info(f"Found existing prefix: {prefix_value}")
            # Get the first prefix from the results
            prefix = list(existing_prefixes)[0]
            
            # Update the prefix with new values if needed
            needs_update = False
            for key, value in defaults.items():
                if getattr(prefix, key) != value:
                    setattr(prefix, key, value)
                    needs_update = True
            
            if needs_update:
                prefix.save()
                logger.info(f"Updated prefix: {prefix_value}")
                
            return prefix, False
    except Exception as e:
        logger.debug(f"Error checking for existing prefix {prefix_value}: {str(e)}")
    
    # If we get here, we need to create the prefix
    try:
        logger.info(f"Creating new prefix: {prefix_value}")
        return nb.ipam.prefixes.create(
            prefix=prefix_value,
            **defaults
        ), True
    except RequestError as e:
        # Check if this is a duplicate prefix error
        if "Duplicate prefix found" in str(e):
            logger.warning(f"Duplicate prefix found: {prefix_value}. Attempting to retrieve existing prefix.")
            # Try to get the existing prefix again
            try:
                existing_prefixes = nb.ipam.prefixes.filter(prefix=prefix_value)
                if existing_prefixes:
                    prefix = list(existing_prefixes)[0]
                    logger.info(f"Retrieved existing prefix: {prefix_value}")
                    return prefix, False
            except Exception as inner_e:
                logger.error(f"Error retrieving duplicate prefix {prefix_value}: {str(inner_e)}")
        
        # Re-raise the original error if we couldn't handle it
        raise

def get_or_create_device_type(nb, model, manufacturer_name, tags):
    """Get or create a device type in Netbox"""
    try:
        device_type = nb.dcim.device_types.get(model=model)
        if device_type:
            return device_type
    except Exception as e:
        logger.debug(f"Error getting device type {model}: {str(e)}")
    
    # Get or create manufacturer
    try:
        manufacturer = nb.dcim.manufacturers.get(name=manufacturer_name)
        if not manufacturer:
            manufacturer = nb.dcim.manufacturers.create(
                name=manufacturer_name,
                slug=manufacturer_name.lower().replace(" ", "-"),
                description=f'Created by Azure sync script'
            )
        manufacturer_id = manufacturer.id
    except Exception as e:
        logger.debug(f"Error getting manufacturer {manufacturer_name}: {str(e)}")
        manufacturer = nb.dcim.manufacturers.create(
            name=manufacturer_name,
            slug=manufacturer_name.lower().replace(" ", "-"),
            description=f'Created by Azure sync script'
        )
        manufacturer_id = manufacturer.id
    
    # Create device type with a slug based on the model name
    model_slug = model.lower().replace(" ", "-")
    return nb.dcim.device_types.create(
        model=model,
        manufacturer=manufacturer_id,
        slug=model_slug,  # Use model name for the slug
        tags=tags
    )


def get_or_create_device_role(nb, name, vm_role, tags):
    """Get or create a device role in Netbox"""
    try:
        role = nb.dcim.device_roles.get(name=name)
        if role:
            return role
    except Exception as e:
        logger.debug(f"Error getting device role {name}: {str(e)}")
    
    return nb.dcim.device_roles.create(
        name=name,
        slug=name.lower().replace(" ", "-"),
        vm_role=vm_role,
        tags=tags
    )

def get_or_create_site(nb, name, description, tags):
    """Get or create a site in Netbox"""
    try:
        site = nb.dcim.sites.get(name=name)
        if site:
            return site
    except Exception as e:
        logger.debug(f"Error getting site {name}: {str(e)}")
    
    return nb.dcim.sites.create(
        name=name,
        status='active',
        slug=name.lower().replace(" ", "-"),
        description=description,
        tags=tags
    )

def sync_to_netbox(all_network_data, netbox_url, netbox_token):
    """Sync Azure network data to Netbox"""
    logger.info(f"Syncing data to Netbox at {netbox_url}")
    nb = api(netbox_url, token=netbox_token)
    session = requests.Session()
    session.verify = False
    nb.http_session = session
    
    # Create a tag for Azure-synced objects
    azure_tag = get_or_create_tag(
        nb,
        tag_name="azure-sync",
        tag_slug="azure-sync",
        tag_description="Synced from Azure"
    )
    
    # Process VNets as prefixes
    for subscription_data in all_network_data:
        subscription_id = subscription_data['subscription_id']
        
        for vnet in subscription_data['vnets']:
            # Create VNet as a prefix
            for address_space in vnet['address_space']:
                vnet_prefix, created = get_or_create_prefix(
                    nb,
                    address_space,
                    {
                        'description': f"Azure VNet: {vnet['name']} (Subscription: {subscription_id})",
                        'status': 'active',
                        'tags': [azure_tag.id]
                    }
                )
                
                action = "Created" if created else "Updated"
                logger.info(f"{action} prefix for VNet {vnet['name']}: {address_space}")
                
                # Process subnets
                for subnet in vnet['subnets']:
                    # Create subnet as a prefix
                    subnet_prefix, created = get_or_create_prefix(
                        nb,
                        subnet['address_prefix'],
                        {
                            'description': f"Azure Subnet: {subnet['name']} (VNet: {vnet['name']})",
                            'status': 'active',
                            'tags': [azure_tag.id],
                            'parent': vnet_prefix.id
                        }
                    )
                    
                    action = "Created" if created else "Updated"
                    logger.info(f"{action} prefix for subnet {subnet['name']}: {subnet['address_prefix']}")
                    
                    # Process devices in subnet
                    for device in subnet['devices']:
                        # Create or update device
                        device_type = get_or_create_device_type(
                            nb,
                            model=f"Azure {device['type'].title()}",
                            manufacturer_name="Microsoft Azure",
                            tags=[azure_tag.id]
                        )
                        
                        device_role = get_or_create_device_role(
                            nb,
                            name=f"Azure {device['type'].title()}",
                            vm_role=device['type'] == 'vm',
                            tags=[azure_tag.id]
                        )
                        
                        site = get_or_create_site(
                            nb,
                            name=f"Azure-{device['location']}",
                            description=f"Azure Region: {device['location']}",
                            tags=[azure_tag.id]
                        )
                        
                        # Try to get existing device
                        device_name = truncate_name(device['name'])
                        try:
                            # First try to find by exact name
                            nb_device = nb.dcim.devices.get(name=device_name, site_id=site.id)
                            if nb_device:
                                logger.info(f"Found existing device: {device_name}")
                            else:
                                # Create new device
                                nb_device = nb.dcim.devices.create(
                                    name=device_name,
                                    device_type=device_type.id,
                                    role=device_role.id,
                                    site=site.id,
                                    status='active',
                                    tags=[azure_tag.id]
                                )
                                logger.info(f"Created new device: {device_name}")
                        except RequestError as e:
                            # Handle the case where device name already exists in the site
                            if "Device name must be unique per site" in str(e):
                                # Make the name unique by appending a suffix
                                suffix = 1
                                while True:
                                    unique_name = f"{device_name}-{suffix}"
                                    try:
                                        # Check if this name is available
                                        if len(unique_name) > 64:
                                            # Truncate again if needed
                                            unique_name = f"{device_name[:60]}-{suffix}"
                                        
                                        nb_device = nb.dcim.devices.create(
                                            name=unique_name,
                                            device_type=device_type.id,
                                            role=device_role.id,
                                            site=site.id,
                                            status='active',
                                            tags=[azure_tag.id]
                                        )
                                        logger.info(f"Created new device with unique name: {unique_name}")
                                        break
                                    except RequestError as inner_e:
                                        if "Device name must be unique per site" in str(inner_e):
                                            suffix += 1
                                        else:
                                            # Re-raise if it's a different error
                                            raise
                            else:
                                # Re-raise if it's a different error
                                raise
                        except Exception as e:
                            logger.debug(f"Error getting device {device_name}: {str(e)}")
                            try:
                                # Create new device
                                nb_device = nb.dcim.devices.create(
                                    name=device_name,
                                    device_type=device_type.id,
                                    role=device_role.id,
                                    site=site.id,
                                    status='active',
                                    tags=[azure_tag.id]
                                )
                                logger.info(f"Created new device: {device_name}")
                            except RequestError as e:
                                # Handle the case where device name already exists in the site
                                if "Device name must be unique per site" in str(e):
                                    # Make the name unique by appending a suffix
                                    suffix = 1
                                    while True:
                                        unique_name = f"{device_name}-{suffix}"
                                        try:
                                            # Check if this name is available
                                            if len(unique_name) > 64:
                                                # Truncate again if needed
                                                unique_name = f"{device_name[:60]}-{suffix}"
                                            
                                            nb_device = nb.dcim.devices.create(
                                                name=unique_name,
                                                device_type=device_type.id,
                                                role=device_role.id,
                                                site=site.id,
                                                status='active',
                                                tags=[azure_tag.id]
                                            )
                                            logger.info(f"Created new device with unique name: {unique_name}")
                                            break
                                        except RequestError as inner_e:
                                            if "Device name must be unique per site" in str(inner_e):
                                                suffix += 1
                                            else:
                                                # Re-raise if it's a different error
                                                raise
                                else:
                                    # Re-raise if it's a different error
                                    raise

                        # Create interface if it doesn't exist
                        interface_name = "eth0"  # Default interface name
                        try:
                            interface = nb.dcim.interfaces.get(device_id=nb_device.id, name=interface_name)
                            if not interface:
                                interface = nb.dcim.interfaces.create(
                                    device=nb_device.id,
                                    name=interface_name,
                                    type="1000base-t",
                                    mac_address=device['mac_address'] if device['mac_address'] else None,
                                    tags=[azure_tag.id]
                                )
                                logger.info(f"Created interface {interface_name} for device {device_name}")
                            else:
                                logger.info(f"Found existing interface {interface_name} for device {device_name}")
                        except Exception as e:
                            logger.debug(f"Error getting interface {interface_name} for device {device_name}: {str(e)}")
                            interface = nb.dcim.interfaces.create(
                                device=nb_device.id,
                                name=interface_name,
                                type="1000base-t",
                                mac_address=device['mac_address'] if device['mac_address'] else None,
                                tags=[azure_tag.id]
                            )
                            logger.info(f"Created interface {interface_name} for device {device_name}")

                        # Create IP address
                        try:
                            ip_address = nb.ipam.ip_addresses.get(address=f"{device['ip_address']}/32")
                            if ip_address:
                                logger.info(f"Found existing IP address for {device_name}: {device['ip_address']}")
                                
                                # Update the IP if needed
                                if ip_address.assigned_object_id != interface.id or ip_address.assigned_object_type != 'dcim.interface':
                                    ip_address.assigned_object_id = interface.id
                                    ip_address.assigned_object_type = 'dcim.interface'
                                    ip_address.save()
                                    logger.info(f"Updated IP address assignment for {device_name}")
                            else:
                                # Create new IP address
                                ip_address = nb.ipam.ip_addresses.create(
                                    address=f"{device['ip_address']}/32",
                                    description=f"IP for {device_name}",
                                    status='active',
                                    tags=[azure_tag.id],
                                    assigned_object_type='dcim.interface',
                                    assigned_object_id=interface.id
                                )
                                logger.info(f"Created new IP address for {device_name}: {device['ip_address']}")
                        except Exception as e:
                            logger.debug(f"Error getting IP address {device['ip_address']}: {str(e)}")
                            # Create new IP address
                            ip_address = nb.ipam.ip_addresses.create(
                                address=f"{device['ip_address']}/32",
                                description=f"IP for {device_name}",
                                status='active',
                                tags=[azure_tag.id],
                                assigned_object_type='dcim.interface',
                                assigned_object_id=interface.id
                            )
                            logger.info(f"Created new IP address for {device_name}: {device['ip_address']}")

def parse_arguments():
    """Parse command line arguments"""
    parser = argparse.ArgumentParser(description='Sync Azure network data to Netbox')
    parser.add_argument('--netbox-url', help='Netbox URL', default=os.environ.get('NETBOX_URL'))
    parser.add_argument('--netbox-token', help='Netbox API token', default=os.environ.get('NETBOX_TOKEN'))
    parser.add_argument('--interactive', action='store_true', help='Use interactive browser authentication for Azure')
    parser.add_argument('--subscription-id', help='Specific Azure subscription ID to process (default: all accessible subscriptions)')
    return parser.parse_args()

def main():
    """Main function to orchestrate the Azure to Netbox sync"""
    args = parse_arguments()
    
    # Validate Netbox parameters
    if not args.netbox_url or not args.netbox_token:
        logger.error("Netbox URL and token must be provided either as arguments or environment variables")
        sys.exit(1)
    
    try:
        logger.info("Starting Azure to Netbox sync")
        
        # Get Azure credentials
        credential = get_azure_credentials(args.interactive)
        
        # Get subscriptions to process
        if args.subscription_id:
            logger.info(f"Processing only subscription {args.subscription_id}")
            subscriptions = [type('obj', (object,), {
                'subscription_id': args.subscription_id,
                'display_name': f"Subscription {args.subscription_id}"
            })]
        else:
            subscriptions = get_azure_subscriptions(credential)
        
        all_network_data = []
        
        # Process each subscription
        for subscription in subscriptions:
            subscription_id = subscription.subscription_id
            subscription_data = {
                'subscription_id': subscription_id,
                'subscription_name': subscription.display_name,
                'vnets': []
            }
            
            # Get VNets and subnets
            vnets_data = get_vnets_and_subnets(subscription_id, credential)
            
            # Get devices in subnets
            vnets_with_devices = get_devices_in_subnet(subscription_id, credential, vnets_data)
            
            subscription_data['vnets'] = vnets_with_devices
            all_network_data.append(subscription_data)
        
        # Sync to Netbox
        sync_to_netbox(all_network_data, args.netbox_url, args.netbox_token)
        
        logger.info("Azure to Netbox sync completed successfully")
        
    except Exception as e:
        logger.error(f"Error during Azure to Netbox sync: {str(e)}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()
