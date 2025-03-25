# Azure to NetBox Synchronization Tool

This script synchronizes Azure networking components to NetBox, providing an automated way to maintain your NetBox IPAM/DCIM database with accurate information from your Azure environment.

## Overview

The Azure_to_Netbox.py script discovers and synchronizes the following Azure resources to NetBox:

- Virtual Networks (VNets) → NetBox Prefixes
- Subnets → NetBox Prefixes (as children of VNet prefixes)
- Virtual Machines and Network Interfaces → NetBox Devices
- Private IP Addresses → NetBox IP Addresses

## Prerequisites

- Python 3.6+
- Access to Azure subscription(s)
- A running NetBox instance with API access
- Required Python packages (see Installation)

## Installation

1. Clone this repository:
```bash
git clone https://your-repository-url/Azure_to_netbox.git
```

2. Install the required dependencies:
```bash
pip install azure-identity azure-mgmt-subscription azure-mgmt-network azure-mgmt-compute pynetbox requests
```

## Configuration

The script can be configured using command-line arguments or environment variables:

### Environment Variables
- `NETBOX_URL`: URL of your NetBox instance
- `NETBOX_TOKEN`: NetBox API token with write access

### Azure Authentication
The script supports two authentication methods:
1. **Default Azure Credential** (default): Uses the Azure credential chain (environment variables, managed identity, etc.)
2. **Interactive Browser Authentication**: Prompts for login using a web browser

## Usage

### Basic Usage
```bash
python Azure_to_Netbox.py --netbox-url https://your-netbox-instance/ --netbox-token your-netbox-token
```

### Using Environment Variables
```bash
export NETBOX_URL=https://your-netbox-instance/
export NETBOX_TOKEN=your-netbox-token
python Azure_to_Netbox.py
```

### Interactive Azure Authentication
```bash
python Azure_to_Netbox.py --interactive
```

### Specify Subscription
To process only a specific Azure subscription:
```bash
python Azure_to_Netbox.py --subscription-id 00000000-0000-0000-0000-000000000000
```

## Features

- **Automatic Discovery**: Automatically discovers all Azure networking components
- **Resource Mapping**: Maps Azure resources to appropriate NetBox objects
- **Idempotent Operation**: Can be run multiple times safely, updating existing resources
- **Tagging**: Adds "azure-sync" tag to all created/updated objects in NetBox
- **Flexible Authentication**: Supports multiple Azure authentication methods
- **Name Handling**: Automatically handles truncation and uniqueness requirements for device names

## Data Synchronization Details

1. **VNets**: Synced as NetBox prefixes with their CIDR ranges
2. **Subnets**: Synced as child prefixes under their parent VNet prefixes
3. **VMs & Network Interfaces**: Created as devices with appropriate device types and roles
4. **IP Addresses**: Associated with the correct interfaces on devices

## Troubleshooting

- **SSL Certificate Issues**: The script currently disables SSL verification. For production, consider properly configuring SSL certificates.
- **Authentication Errors**: If experiencing Azure authentication issues, try the `--interactive` flag.
- **Rate Limiting**: If hitting NetBox API rate limits, consider adding pauses between operations.
- **Logging**: The script logs operations at INFO level. Review logs for troubleshooting.

## Notes

- The script is designed to be run periodically to keep NetBox updated with the current state of Azure.
- All objects created or updated by the script receive an "azure-sync" tag for identification.
