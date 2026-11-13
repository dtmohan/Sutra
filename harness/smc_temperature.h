/* Read-only AppleSMC temperature adapter.
 * Protocol layout reference: beltex/SMCKit (MIT; vendor/smckit/LICENSE.txt).
 * Sensor-key provenance: exelban/stats, Modules/Sensors/values.swift, M5 family.
 * Restricted to the Mac17,13 / Apple M5 Pro mapping verified on this host.
 * These are thermal sensor zones, not a claim of one sensor per physical core.
 */
#include <IOKit/IOKitLib.h>
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <math.h>

typedef struct {uint8_t major,minor,build,reserved;uint16_t release;} KarmaSMCVersion;
typedef struct {uint16_t version,length;uint32_t cpu,gpu,memory;} KarmaSMCLimits;
typedef struct {uint32_t size,type;uint8_t attributes;} KarmaSMCInfo;
typedef struct {
    uint32_t key;KarmaSMCVersion version;KarmaSMCLimits limits;KarmaSMCInfo info;
    uint8_t result,status,command;uint32_t value;uint8_t bytes[32];
} KarmaSMCPacket;
_Static_assert(sizeof(KarmaSMCPacket)==80,"AppleSMC packet ABI");
_Static_assert(offsetof(KarmaSMCPacket,bytes)==48,"AppleSMC byte offset");

static uint32_t KarmaFourCC(const char *s) {
    return (uint32_t)(uint8_t)s[0]<<24|(uint32_t)(uint8_t)s[1]<<16|(uint32_t)(uint8_t)s[2]<<8|(uint8_t)s[3];
}
static BOOL KarmaSMCRead(io_connect_t connection,NSString *key,double *value) {
    KarmaSMCPacket request={0},response={0};size_t size=sizeof(response);
    request.key=KarmaFourCC(key.UTF8String);request.command=9; // Read key metadata.
    kern_return_t rc=IOConnectCallStructMethod(connection,2,&request,sizeof(request),&response,&size);
    if(rc!=KERN_SUCCESS||size!=sizeof(response)||response.result!=0)return NO;
    // This verified profile uses IEEE-754 little-endian floats only.
    if(response.info.size!=4||response.info.type!=KarmaFourCC("flt "))return NO;
    request.info=response.info;request.command=5; // Read bytes. No write command exists here.
    memset(&response,0,sizeof(response));size=sizeof(response);
    rc=IOConnectCallStructMethod(connection,2,&request,sizeof(request),&response,&size);
    if(rc!=KERN_SUCCESS||size!=sizeof(response)||response.result!=0)return NO;
    float measured;memcpy(&measured,response.bytes,sizeof(measured));
    if(!isfinite(measured)||measured<=0||measured>150)return NO;
    *value=measured;return YES;
}
static NSString *KarmaSysctlString(const char *key) {
    char buffer[128]={0};size_t size=sizeof(buffer);
    if(sysctlbyname(key,buffer,&size,NULL,0)!=0||size==0||size>sizeof(buffer))return @"unknown";
    buffer[sizeof(buffer)-1]=0;return [NSString stringWithUTF8String:buffer]?:@"unknown";
}
static NSDictionary *KarmaSMCTemperatures(void) {
    NSString *machine=KarmaSysctlString("hw.model"),*chip=KarmaSysctlString("machdep.cpu.brand_string");
    NSMutableDictionary *sample=[@{@"source":@"AppleSMC",@"profile":@"mac17-13-m5-pro-v1",@"machine":machine,@"chip":chip,
        @"sampled_at":@([[NSDate date] timeIntervalSince1970]),@"read_only":@YES,@"cpu":@[],@"gpu":@[]} mutableCopy];
    if(![machine isEqualToString:@"Mac17,13"]||![chip isEqualToString:@"Apple M5 Pro"]){sample[@"status"]=@"unsupported_machine";return sample;}
    io_service_t service=IOServiceGetMatchingService(kIOMainPortDefault,IOServiceMatching("AppleSMC"));
    if(!service){sample[@"status"]=@"driver_unavailable";return sample;}
    io_connect_t connection=0;kern_return_t rc=IOServiceOpen(service,mach_task_self(),0,&connection);IOObjectRelease(service);
    if(rc!=KERN_SUCCESS){sample[@"status"]=@"open_failed";sample[@"driver_error"]=@(rc);return sample;}
    NSDictionary *groups=@{
        @"cpu":@[@"Tp00",@"Tp04",@"Tp08",@"Tp0C",@"Tp0G",@"Tp0K",@"Tp0O",@"Tp0R",@"Tp0U",@"Tp0X",@"Tp0a",@"Tp0d",@"Tp0g",@"Tp0j",@"Tp0m",@"Tp0p",@"Tp0u",@"Tp0y"],
        @"gpu":@[@"Tg0U",@"Tg0X",@"Tg0d",@"Tg0g",@"Tg0j",@"Tg1Y",@"Tg1c",@"Tg1g"]};
    for(NSString *group in @[@"cpu",@"gpu"]){
        NSMutableArray *readings=[NSMutableArray array];
        for(NSString *key in groups[group]){double value;if(KarmaSMCRead(connection,key,&value)) [readings addObject:@{@"key":key,@"celsius":@(value)}];}
        sample[group]=readings;
    }
    IOServiceClose(connection);sample[@"sampled_at"]=@([[NSDate date] timeIntervalSince1970]);sample[@"status"]=@"sampled";
    return sample;
}
