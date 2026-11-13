#import <Foundation/Foundation.h>
#include <sys/sysctl.h>
#include <assert.h>
#include <IOKit/IOKitLib.h>
kern_return_t KarmaFakeSMCMethod(mach_port_t,uint32_t,const void *,size_t,void *,size_t *);
#define IOConnectCallStructMethod KarmaFakeSMCMethod
#include "../harness/smc_temperature.h"
static int fault=0,calls=0;
kern_return_t KarmaFakeSMCMethod(mach_port_t c,uint32_t selector,const void *input,size_t inputSize,void *output,size_t *outputSize){
    const KarmaSMCPacket *in=input;KarmaSMCPacket *out=output;memset(out,0,sizeof(*out));calls++;
    assert(selector==2&&inputSize==80&&*outputSize==80);assert(in->command==9||in->command==5);
    if(fault==1)return kIOReturnNotPrivileged;
    if(fault==2){out->result=132;return KERN_SUCCESS;}
    if(fault==3){*outputSize=1;return KERN_SUCCESS;}
    if(in->command==9){out->info.size=fault==4?32:4;out->info.type=KarmaFourCC(fault==5?"ui32":"flt ");}
    else{float value=fault==6?NAN:fault==7?INFINITY:fault==8?0:fault==9?151:45.25;memcpy(out->bytes,&value,4);}
    return KERN_SUCCESS;
}
int main(){@autoreleasepool{
    double value=-1;assert(KarmaSMCRead(0,@"Tp00",&value));assert(value==45.25&&calls==2);
    for(fault=1;fault<=9;fault++){calls=0;value=-1;assert(!KarmaSMCRead(0,@"Tp00",&value));assert(value==-1);assert(calls<=2);}
    puts("SMC read-only ABI, float decoding, missing key, permission error, short response, invalid type and invalid numeric tests passed.");
}}
