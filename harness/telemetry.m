#import <Foundation/Foundation.h>
#include <mach/mach.h>
#include <sys/sysctl.h>
#include <unistd.h>
#include "smc_temperature.h"

// Read-only, one JSON sample per second. SMC sensor values are advisory.
int main(int argc,const char *argv[]) {
    BOOL once=argc==2&&strcmp(argv[1],"--once")==0;
    uint64_t prior[CPU_STATE_MAX] = {0};
    BOOL havePrior = NO;
    while (1) {
        @autoreleasepool {
            NSMutableDictionary *d = [NSMutableDictionary dictionary];
            d[@"wall_time"] = @([[NSDate date] timeIntervalSince1970]);
            d[@"thermal_state"] = @([NSProcessInfo processInfo].thermalState);
            d[@"thermal_source"] = @"NSProcessInfo.thermalState";
            d[@"temperatures"] = KarmaSMCTemperatures();
            d[@"low_power"] = @([NSProcessInfo processInfo].lowPowerModeEnabled);
            d[@"physical_bytes"] = @([NSProcessInfo processInfo].physicalMemory);
            int pressure = -1; size_t len = sizeof(pressure);
            if (sysctlbyname("kern.memorystatus_vm_pressure_level", &pressure, &len, NULL, 0) == 0)
                d[@"memory_pressure_raw"] = @(pressure);
            struct xsw_usage swap; len = sizeof(swap);
            if (sysctlbyname("vm.swapusage", &swap, &len, NULL, 0) == 0)
                d[@"swap_used_bytes"] = @(swap.xsu_used);
            vm_statistics64_data_t vm;
            mach_msg_type_number_t count = HOST_VM_INFO64_COUNT;
            if (host_statistics64(mach_host_self(), HOST_VM_INFO64, (host_info64_t)&vm, &count) == KERN_SUCCESS) {
                vm_size_t page; host_page_size(mach_host_self(), &page);
                d[@"page_size"] = @(page);
                d[@"free_bytes"] = @((uint64_t)vm.free_count * page);
                d[@"inactive_bytes"] = @((uint64_t)vm.inactive_count * page);
                d[@"wired_bytes"] = @((uint64_t)vm.wire_count * page);
                d[@"compressed_bytes"] = @((uint64_t)vm.compressor_page_count * page);
                d[@"swapouts_pages"] = @(vm.swapouts);
            }
            host_cpu_load_info_data_t cpu;
            count = HOST_CPU_LOAD_INFO_COUNT;
            if (host_statistics(mach_host_self(), HOST_CPU_LOAD_INFO, (host_info_t)&cpu, &count) == KERN_SUCCESS) {
                uint64_t delta[CPU_STATE_MAX], total = 0;
                for (int i=0; i<CPU_STATE_MAX; i++) {
                    delta[i] = (uint32_t)(cpu.cpu_ticks[i] - prior[i]);
                    total += delta[i]; prior[i] = cpu.cpu_ticks[i];
                }
                if (havePrior && total) d[@"cpu_busy_pct"] = @(100.0 * (total-delta[CPU_STATE_IDLE]) / total);
                havePrior = YES;
            }
            NSData *json = [NSJSONSerialization dataWithJSONObject:d options:0 error:nil];
            fwrite(json.bytes, 1, json.length, stdout); fputc('\n', stdout); fflush(stdout);
        }
        if(once)break;
        sleep(1);
    }
}
