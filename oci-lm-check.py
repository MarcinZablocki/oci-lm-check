#!/usr/bin/env python3
from collections import namedtuple
from email.policy import default
import oci 
import click
from rich.console import Console
from rich.table import Table
from rich.progress import Progress
from prompt_toolkit import completion, PromptSession
from prompt_toolkit.shortcuts import checkboxlist_dialog
from typing import List

console = Console()

class CompartmentsCompleter(completion.Completer):
    
    def __init__(self, compartments: List[oci.identity.models.Compartment]) -> None:
        super().__init__()
        self.compartments = compartments
        
    def get_completions(self, document, complete_event):
        for c in self.compartments:
            yield completion.Completion(c.id, 0, display=c.name)
            
def compartments_selector(compartments):
  
  all = oci.identity.models.Compartment(name="ALL", id="ALL")
  
  c_list = []+compartments
  c_list.insert(0, all)
  
  results = checkboxlist_dialog(
    title="Compartments",
    text="Select compartments to read",
    values=[(c, c.name) for c in c_list]
).run()
  
  if any(x.name == "ALL" for x in results):
    return compartments
  else:   
    return results

def region_selector(regions):
  
  all = oci.identity.models.RegionSubscription(region_name="ALL")
  
  r_list = []+regions
  r_list.sort(key=lambda x: x.region_name)
  r_list.insert(0, all)
  
  results = checkboxlist_dialog(
    title="Regions",
    text="Select regions to read",
    values=[(c, c.region_name) for c in r_list]
).run()
  
  if any(x.region_name == "ALL" for x in results):
    return regions
  else: 
    return results
            
class InstancesCompleter(completion.Completer):
    
    def __init__(self, instances: List[oci.core.models.Instance]) -> None:
        super().__init__()
        self.instances = instances
        
    def get_completions(self, document, complete_event):
        for i in self.instances:
            yield completion.Completion(i.id, 0, display=i.display_name)
    
def get_regions(identity: oci.identity.IdentityClient, config: oci.config): 
    regions = oci.pagination.list_call_get_all_results(identity.list_region_subscriptions, config.get('tenancy'), retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
    return regions

def get_instances(compute_client, compartment): 
    instances = oci.pagination.list_call_get_all_results(compute_client.list_instances, compartment, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
    return instances

def get_compartments(profile): 
    config = oci.config.from_file(profile_name=profile)
    identity = oci.identity.IdentityClient(config, profile_name=profile)

    compartments = oci.pagination.list_call_get_all_results(identity.list_compartments, config.get('tenancy'), access_level='ACCESSIBLE', compartment_id_in_subtree=True, lifecycle_state='ACTIVE', retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
        
    return compartments

def get_compartments(profile, compartment):
    config = oci.config.from_file(profile_name=profile)
    identity = oci.identity.IdentityClient(config, profile_name=profile)

    c_list = []
    for c in compartment.split(","):
      c_list.append(identity.get_compartment(compartment_id=c).data)

    return c_list

@click.command()
@click.option("--compartment_id", "-c", "compartment", help="Comma separated compartment IDs.")
@click.option("--regions", "-r", "region", help="Comma separated regions to query. E.g: us-ashburn-1,us-phoenix-1")
@click.option("--profile", "-p", help="config file profile", default='DEFAULT')
@click.option("-b", help="batch mode", is_flag=True)
def main(compartment, region, profile, b):
  
    c_list = []  
    r_list = []
    
    if not compartment and not b:
        compartments = get_compartments(profile)
        c_list = compartments_selector(compartments)
    elif compartment: 
      c_list = get_compartments(profile, compartment)
    else: 
      compartments = get_compartments(profile)
      c_list = compartments
    
    if not region and not b: 
      config = oci.config.from_file(profile_name=profile)
      regions = get_regions(oci.identity.IdentityClient(config), config)
      r_list = region_selector(regions)
    
    if region: 
      # not full implementation of RegionSubscription but we're using only the region_name
      for i in region.split(","): 
        r = oci.identity.models.RegionSubscription(region_name=i) 
        r_list.append(r)
        
    print(r_list)
        
    run(c_list, profile, r_list)
    
def check(instance: oci.core.models.Instance, compute_client: oci.core.ComputeClient):
    
  if "BM." in instance.shape:
      lm = "No - Bare Metal"
      return lm
  
  else: 
  
    if instance.launch_options.network_type == 'PARAVIRTUALIZED': 
        if instance.availability_config.is_live_migration_preferred is not False: 
                lm = "Yes"
        else:
                lm = "No - disabled"
                return lm
        
    else:
        #print(f"Not paravirtualized network {instance.display_name}")
        lm = "No (NIC type)"
        return lm
    
    if instance.dedicated_vm_host_id: 
        lm = "No (DVH)"
        return lm
    
    if "VM.Standard.A1." in instance.shape:
        lm = "No (ARM instance)"
        return lm
        
    if instance.shape_config.local_disks > 0: 
        #print(f"Local disks detected {instance.display_name}")
        lm = "No (DenseIO)"
        return lm
        
    if instance.shape_config.gpus > 0: 
        #print(f"GPUS detected {instance.display_name}")
        lm = "No (GPU)"
        return lm
    
    image = compute_client.get_image(instance.image_id, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
    if image.operating_system == "Windows":
        lm = "No (Windows)"
        return lm

  return lm      

def collect(compute_client, vcn_client, compartment, region, instance_table): 
  
  instances = get_instances(compute_client, compartment.id)
  vnic_attachments = oci.pagination.list_call_get_all_results(compute_client.list_vnic_attachments, compartment.id, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
  volume_attachments = oci.pagination.list_call_get_all_results(compute_client.list_volume_attachments, compartment.id, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data
  
  instance_list = {}

  for i in instances:
    if i.lifecycle_state == "TERMINATED": 
      continue
        
    v = oci.core.models.Vnic()
    if i.lifecycle_state == "RUNNING": 
      s = ":white_check_mark:" 
      va = [ x for x in vnic_attachments if i.id in x.instance_id ]
      vs = []
      bva = [ x for x in volume_attachments if i.id in x.instance_id ]
      bv = []
      
      if len(va) > 0:
        for v in va: 
          vs.append(vcn_client.get_vnic(v.vnic_id, retry_strategy=oci.retry.DEFAULT_RETRY_STRATEGY).data)
                
        if len(bva) > 0:
          for a in bva: 
            bv.append(bva) 
                     
        lm = check(i, compute_client)
        
        instance_table.add_row(compartment.name, i.id, i.display_name, i.region, i.shape, vs[0].private_ip, vs[0].public_ip, i.lifecycle_state, i.launch_options.network_type, str(len(bva)),i.time_maintenance_reboot_due, str(lm))
        
    elif i.lifecycle_state == "STOPPED": 
        s = ":stop_sign:"
        instance_table.add_row(i.id, i.display_name, i.shape, "", "", i.lifecycle_state, i.launch_options.network_type, "N/A","N/A", "N/A")

    elif i.lifecycle_state == "STOPPING": 
        instance_table.add_row(i.id, i.display_name, i.shape, "", "", i.lifecycle_state, i.launch_options.network_type, "N/A","N/A", "N/A")
        s = ":stop_button:"
 
  return instance_table

def run(compartments, profile, r): 
    config = oci.config.from_file(profile_name=profile)
    identity = oci.identity.IdentityClient(config)
    
    instance_table = Table(show_header=True, header_style="bold magenta")
    instance_table.add_column("COMPARTMENT")
    instance_table.add_column("ID")
    instance_table.add_column("NAME")
    instance_table.add_column("REGION")
    instance_table.add_column("SHAPE")
    instance_table.add_column("PRIVATE IP")
    instance_table.add_column("PUBLIC IP")
    instance_table.add_column("STATE")
    instance_table.add_column("NIC TYPE")
    instance_table.add_column("VOLUMES")
    instance_table.add_column("MAINTENANCE")  
    instance_table.add_column("LIVE MIGRATION")  
    
    if len(r) == 0: 
      regions = get_regions(identity, config)
    else:
      regions = r
    
    with Progress() as progress:
      task = progress.add_task(f"[bold green]Collecting data")
    
      total = len(regions) * len(compartments)
      progress.update(task, total=total)
        
        
      for region in regions: 
          config['region'] = region.region_name
          compute_client = oci.core.ComputeClient(config)
          vcn_client = oci.core.VirtualNetworkClient(config)

          for compartment in compartments: 
            progress.update(task, description=f"[bold green]Collecting data | {compartment.name}  | {region.region_name} ")
            instance_table = collect(compute_client, vcn_client, compartment, region, instance_table)
            progress.update(task, advance=1)
                      
    console.print(instance_table)

if __name__ == "__main__": 
    main()
